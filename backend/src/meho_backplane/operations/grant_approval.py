# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may approve a change that gives a login more rights.

A connector registers the operations that add a login to a group, give a
login a role, or change the attributes of a group (``register_grant_op``).
For these, MEHO checks when a person approves:

* The login and the group the change is for must be known. The connector
  resolves a user name or a group name to its id when the request is
  parked, the same way the handler will when the change runs. When it
  could not, the request cannot be approved: MEHO does not know whose
  rights the change would raise. The requester sends a new request.
* The login the change is for is never the approver. A person cannot raise
  their own rights with their own approval, in any group and at any role
  level. A second login of the same person is a different login, so it is
  a different approver in this check.
* A change of a group's attributes changes the rights of everyone in the
  group. In MEHO's own login realm, a person who is in the group never
  approves it, and the request cannot be approved when MEHO could not
  read who is in the group.
* In MEHO's own login realm, the change always needs a second person who
  is a tenant admin (a platform admin for the platform admin group or
  role). No self-approval rule covers it, also not the one of the
  MEHO-SA-Operators group.

The connector describes each parked request in
``proposed_effect["preview"]["grant"]``: ``user_id`` (the login the change
is for, when known), ``group_id`` (the group, when known), ``login_realm``,
``platform_admin`` and, for a group attribute change, ``members`` (the ids
of the logins in the group, ``None`` when they could not be read). A
request without that description counts as a change in the login realm
that needs a platform admin, so a missing description never makes the
check weaker.

Right before an approved change runs, :func:`grant_changed_since_request`
resolves the login and the group again against the live system. When a
name now names another login or group, or cannot be read, the change does
not run. So the approval binds the change to the ids the approver saw,
not to a name that can move while the request waits. The same check
compares the facts that decided who may approve with the live system, so
they also bind the run:

* A change of a group's attributes in the login realm does not run when
  the person who approved it is in the group now, or when MEHO cannot read
  who is in the group. A person can join the group after the request was
  made, so the list saved with the request is not enough.
* The change does not run when it now needs a stricter approval than the
  one it got: the realm became a login realm, the group became the platform
  admin group, or MEHO cannot read which case it is.

The decision audit row names the login the change is for and says whether
it is the approver (:func:`grant_audit_fields`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.auth.operator import Operator, TenantRole

if TYPE_CHECKING:
    from meho_backplane.db.models import ApprovalRequest

__all__ = [
    "GRANT_REASON_APPROVER_IN_GROUP",
    "GRANT_REASON_CHANGED",
    "GRANT_REASON_STRICTER",
    "GRANT_REASON_UNREADABLE",
    "GrantFacts",
    "check_grant_approval",
    "grant_audit_fields",
    "grant_changed_since_request",
    "grant_facts",
    "register_grant_op",
]

_log = structlog.get_logger(__name__)

#: Why a run was refused: the name now names another login or group.
GRANT_REASON_CHANGED: Final[str] = (
    "The login or group this change is for is not the one on the approval card: "
    "its name now names another one. The operation did not run. Send a new request."
)

#: Why a run was refused: the login or group could not be read again.
GRANT_REASON_UNREADABLE: Final[str] = (
    "MEHO could not read the login or group this change is for, or who is in the "
    "group, so it could not check that the change is still the one on the approval "
    "card. The operation did not run. Send a new request."
)

#: Why a run was refused: the approver joined the group while the request waited.
GRANT_REASON_APPROVER_IN_GROUP: Final[str] = (
    "The person who approved this change is now in the group it changes, so the "
    "change is for their own login too. The operation did not run. Send a new "
    "request, and ask a second person who is not in the group to approve it."
)

#: Why a run was refused: the change needs a stricter approval than it got.
GRANT_REASON_STRICTER: Final[str] = (
    "This change now needs a stricter approval than the one it got: the realm or "
    "group it is for changed while the request waited, or MEHO could not read it. "
    "The operation did not run. Send a new request."
)


@dataclass(frozen=True)
class _GrantOp:
    #: The params that can hold the id of the login the change is for.
    user_id_params: tuple[str, ...]
    #: The params that can hold the id of the group the change is for.
    group_id_params: tuple[str, ...]


#: op_id -> where its params carry the login and the group.
_GRANT_OPS: dict[str, _GrantOp] = {}


def register_grant_op(
    op_id: str,
    *,
    user_id_params: tuple[str, ...] = (),
    group_id_params: tuple[str, ...] = (),
) -> None:
    """Record that *op_id* changes the rights of a login or a group.

    *user_id_params* is empty for a change of a whole group;
    *group_id_params* is empty for a change that names no group.
    """
    _GRANT_OPS[op_id] = _GrantOp(user_id_params, group_id_params)


@dataclass(frozen=True)
class GrantFacts:
    """What an approval needs to know about a change of rights."""

    #: The change raises the rights of one login.
    for_login: bool
    #: The change names a group (a member add, or a group attribute change).
    for_group: bool
    user_id: str | None
    group_id: str | None
    login_realm: bool
    platform_admin: bool
    #: The logins in the group, for a group attribute change. ``None`` when
    #: they could not be read, or when the change is for one login.
    members: tuple[str, ...] | None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first_text(params: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    return next((v for k in keys if (v := _text(params.get(k)))), None)


def _member_ids(raw: Any) -> tuple[str, ...] | None:
    return tuple(m for m in raw if isinstance(m, str)) if isinstance(raw, list) else None


def _stamp(request: ApprovalRequest) -> dict[str, Any]:
    preview = (request.proposed_effect or {}).get("preview")
    stamp = preview.get("grant") if isinstance(preview, dict) else None
    return stamp if isinstance(stamp, dict) else {}


def grant_facts(request: ApprovalRequest) -> GrantFacts | None:
    """The facts of *request*, or ``None`` when it is not a change of rights."""
    op = _GRANT_OPS.get(request.op_id)
    if op is None:
        return None
    params = request.params if isinstance(request.params, dict) else {}
    stamp = _stamp(request)
    return GrantFacts(
        for_login=bool(op.user_id_params),
        for_group=bool(op.group_id_params),
        user_id=_first_text(params, op.user_id_params) or _text(stamp.get("user_id")),
        group_id=_first_text(params, op.group_id_params) or _text(stamp.get("group_id")),
        login_realm=stamp.get("login_realm") is not False,
        platform_admin=stamp.get("platform_admin") is not False,
        members=_member_ids(stamp.get("members")),
    )


def _same_login(first: str | None, second: str) -> bool:
    if first is None:
        return False
    try:
        return uuid.UUID(first) == uuid.UUID(second)
    except ValueError:
        return first == second


def check_grant_approval(operator: Operator, request: ApprovalRequest) -> None:
    """Raise :class:`ApprovalRoleFloorError` when *operator* may not approve *request*."""
    facts = grant_facts(request)
    if facts is None:
        return
    _check_who_the_change_is_for(operator, request, facts)
    if not facts.login_realm:
        return
    if facts.for_group and not facts.for_login:
        _check_group_members(operator, request, facts)
    _check_login_realm_approver(operator, request, facts)


def _check_who_the_change_is_for(
    operator: Operator, request: ApprovalRequest, facts: GrantFacts
) -> None:
    """The login and the group are known, and the login is not the approver."""
    if facts.for_login and facts.user_id is None:
        raise _refusal(
            request,
            operator,
            "MEHO could not establish which login this change is for when the request "
            "was made. It cannot be approved. Send a new request",
        )
    if facts.for_group and facts.group_id is None:
        raise _refusal(
            request,
            operator,
            "MEHO could not establish which group this change is for when the request "
            "was made. It cannot be approved. Send a new request",
        )
    if _same_login(facts.user_id, operator.sub):
        raise _refusal(
            request,
            operator,
            "this change is for your own login. A second person must approve it, "
            "and a second login of the same person does not count",
        )


def _check_login_realm_approver(
    operator: Operator, request: ApprovalRequest, facts: GrantFacts
) -> None:
    """In the login realm: a second person who is a tenant admin, or a platform admin."""
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


def _check_group_members(operator: Operator, request: ApprovalRequest, facts: GrantFacts) -> None:
    """A group change in the login realm: the approver is not in the group."""
    if facts.members is None:
        raise _refusal(
            request,
            operator,
            "MEHO could not read who is in this group when the request was made, so it "
            "cannot tell whether the change is for your own login. It cannot be approved. "
            "Send a new request",
        )
    if any(_same_login(member, operator.sub) for member in facts.members):
        raise _refusal(
            request,
            operator,
            "you are in this group, so this change is for your own login too. "
            "A second person who is not in the group must approve it",
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
    if facts.for_group:
        fields["grant_group_id"] = facts.group_id
    username = _text(params.get("username"))
    if username is not None:
        fields["grant_username"] = username
    return fields


async def grant_changed_since_request(
    operator: Operator,
    request: ApprovalRequest,
    target: Any,
    params: dict[str, Any],
) -> str | None:
    """Why the run must be refused, or ``None`` when the change is for the approved ids.

    Builds the approval card again against the live system
    (:func:`~meho_backplane.operations.approval_object_check.live_card`)
    and compares the login and the group it names with the ones the
    approver saw. Returns :data:`GRANT_REASON_UNREADABLE` when the card
    does not build, names no login or group, or cannot say who is in the
    group, :data:`GRANT_REASON_CHANGED` when a name now names another id,
    :data:`GRANT_REASON_STRICTER` when the change now needs a stricter
    approval, :data:`GRANT_REASON_APPROVER_IN_GROUP` when the approver is
    now in the group the change is for, and ``None`` for a request that
    is not a change of rights or for which all of this still holds.
    """
    facts = grant_facts(request)
    if facts is None:
        return None
    from meho_backplane.operations.approval_object_check import live_card

    card = await live_card(
        operator,
        connector_id=request.connector_id,
        op_id=request.op_id,
        target=target,
        params=params,
    )
    preview = card.get("preview") if isinstance(card, dict) else None
    live = preview.get("grant") if isinstance(preview, dict) else None
    log_fields = {
        "approval_request_id": str(request.id),
        "op_id": request.op_id,
        "connector_id": request.connector_id,
    }
    if not isinstance(live, dict):
        _log.warning("approval_resume_grant_unreadable", **log_fields)
        return GRANT_REASON_UNREADABLE
    reason = (
        _id_moved(facts, live)
        or _stricter_now(facts, live)
        or _approver_joined_group(request, facts, live)
    )
    if reason is not None:
        kind = "unreadable" if reason == GRANT_REASON_UNREADABLE else "changed"
        _log.warning(f"approval_resume_grant_{kind}", **log_fields)
    return reason


def _id_moved(facts: GrantFacts, live: dict[str, Any]) -> str | None:
    """Why the login or the group on the live card is not the approved one."""
    checks: list[tuple[str | None, str | None]] = []
    if facts.for_login:
        checks.append((facts.user_id, _text(live.get("user_id"))))
    if facts.for_group:
        checks.append((facts.group_id, _text(live.get("group_id"))))
    for approved, now in checks:
        if now is None:
            return GRANT_REASON_UNREADABLE
        if approved is None or not _same_login(approved, now):
            return GRANT_REASON_CHANGED
    return None


def _stricter_now(facts: GrantFacts, live: dict[str, Any]) -> str | None:
    """Why the live change needs a stricter approval than the one it got."""
    realm_is_stricter = not facts.login_realm and live.get("login_realm") is not False
    group_is_stricter = not facts.platform_admin and live.get("platform_admin") is not False
    return GRANT_REASON_STRICTER if realm_is_stricter or group_is_stricter else None


def _approver_joined_group(
    request: ApprovalRequest, facts: GrantFacts, live: dict[str, Any]
) -> str | None:
    """Why the person who approved a group change is not outside the group now."""
    if not facts.login_realm or facts.for_login or not facts.for_group:
        return None
    members = _member_ids(live.get("members"))
    if members is None or request.reviewed_by is None:
        return GRANT_REASON_UNREADABLE
    if any(_same_login(member, request.reviewed_by) for member in members):
        return GRANT_REASON_APPROVER_IN_GROUP
    return None
