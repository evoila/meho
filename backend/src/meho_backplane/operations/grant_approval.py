# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may approve a change that gives a login more rights.

A connector registers the operations that add a login to a group, give a
login a role, or change the attributes of a group (``register_grant_op``).
For these, MEHO checks two things when a person approves:

* The login the change is for is never the approver. A person cannot raise
  their own rights with their own approval, in any group and at any role
  level. A second login of the same person is a different login, so it is
  a different approver in this check.
* In MEHO's own login realm, the change always needs a second person who
  is a tenant admin (a platform admin for the platform admin group or
  role). No self-approval rule covers it, also not the one of the
  MEHO-SA-Operators group.

The connector describes each parked request in
``proposed_effect["preview"]["grant"]``: ``user_id`` (the login the change
is for, when known), ``login_realm`` and ``platform_admin``. A request
without that description counts as a change in the login realm that needs a
platform admin, so a missing description never makes the check weaker.

The decision audit row names the login the change is for and says whether
it is the approver (:func:`grant_audit_fields`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator, TenantRole

if TYPE_CHECKING:
    from meho_backplane.db.models import ApprovalRequest

__all__ = [
    "GrantFacts",
    "check_grant_approval",
    "grant_audit_fields",
    "grant_facts",
    "register_grant_op",
]

#: op_id -> the params that can hold the id of the login the change is for.
_GRANT_OPS: dict[str, tuple[str, ...]] = {}


def register_grant_op(op_id: str, *, user_id_params: tuple[str, ...] = ()) -> None:
    """Record that *op_id* changes the rights of a login or a group."""
    _GRANT_OPS[op_id] = user_id_params


@dataclass(frozen=True)
class GrantFacts:
    """What an approval needs to know about a change of rights."""

    user_id: str | None
    login_realm: bool
    platform_admin: bool


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def grant_facts(request: ApprovalRequest) -> GrantFacts | None:
    """The facts of *request*, or ``None`` when it is not a change of rights."""
    id_params = _GRANT_OPS.get(request.op_id)
    if id_params is None:
        return None
    params = request.params if isinstance(request.params, dict) else {}
    preview = (request.proposed_effect or {}).get("preview")
    stamp = preview.get("grant") if isinstance(preview, dict) else None
    stamp = stamp if isinstance(stamp, dict) else {}
    user_id = next((v for k in id_params if (v := _text(params.get(k)))), None)
    if user_id is None:
        user_id = _text(stamp.get("user_id"))
    return GrantFacts(
        user_id=user_id,
        login_realm=stamp.get("login_realm") is not False,
        platform_admin=stamp.get("platform_admin") is not False,
    )


def _same_login(first: str | None, second: str) -> bool:
    if first is None:
        return False
    try:
        return uuid.UUID(first) == uuid.UUID(second)
    except ValueError:
        return first == second


def check_grant_approval(operator: Operator, request: ApprovalRequest) -> None:
    """Raise :class:`ApprovalGrantError` when *operator* may not approve *request*."""
    facts = grant_facts(request)
    if facts is None:
        return
    if _same_login(facts.user_id, operator.sub):
        raise _refusal(
            request,
            operator,
            "this change is for your own login. A second person must approve it, "
            "and a second login of the same person does not count",
        )
    if not facts.login_realm:
        return
    if operator.sub == request.principal_sub:
        raise _refusal(
            request,
            operator,
            "it changes who has rights in MEHO's own login realm. A second person must approve it",
        )
    if facts.platform_admin:
        if not operator.platform_admin:
            raise _refusal(
                request,
                operator,
                "it changes who has rights in MEHO's own login realm "
                "(the platform admin group or role, or a group MEHO could not read). "
                "A second person who is a platform admin must approve it",
            )
    elif operator.tenant_role != TenantRole.TENANT_ADMIN:
        raise _refusal(
            request,
            operator,
            "it changes who has rights in MEHO's own login realm. "
            "A second person who is a tenant admin must approve it",
        )


def _refusal(request: ApprovalRequest, operator: Operator, denial: str) -> Exception:
    from meho_backplane.operations.approval_queue import ApprovalRoleFloorError

    return ApprovalRoleFloorError(
        request.id,
        operator_sub=operator.sub,
        role=operator.tenant_role.value,
        denial=denial,
    )


def grant_audit_fields(request: ApprovalRequest, operator: Operator) -> dict[str, Any]:
    """The fields for the decision audit row: who the change is for."""
    facts = grant_facts(request)
    if facts is None:
        return {}
    params = request.params if isinstance(request.params, dict) else {}
    fields: dict[str, Any] = {
        "grant_user_id": facts.user_id,
        "grant_for_approver": _same_login(facts.user_id, operator.sub),
        "grant_in_login_realm": facts.login_realm,
    }
    username = _text(params.get("username"))
    if username is not None:
        fields["grant_username"] = username
    return fields
