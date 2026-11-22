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
* Only an admin approves it (see "The rights-change mark" below). The
  admin may approve a change of their own login or of a group they are
  in, but never a ``secret.move`` that sets the password of their own
  login (:data:`FOR_LOGIN`). Outside MEHO's own login realm, the admin may
  also approve a change they asked for themselves.
* In MEHO's own login realm, every change of rights needs a platform
  admin, also one that sets only harmless keys. When the change can give
  platform admin rights (the connector decides this from what the group
  or role gives, ``platform_admin``, not from its name), the refusal and
  the approval card say why (:func:`platform_admin_reason`). There the
  login that asked approves the change only when it asked for it in the
  web console (:data:`ASKED_IN_WEB_CONSOLE`) and approves it there too. A
  request made with a token, the command line, MCP or by an agent is
  never approved by the login that asked. A person behind the agent,
  service or runner login that asked may approve it.
* An operation registered with ``applies`` is a change of rights here only
  when its params pass that check (``secret.move`` with a Keycloak side).
* An operation registered with ``per_call=False`` is checked here too, but
  it is not marked for per-call approval, and outside the login realm it
  is not a change of rights.

The connector describes each parked request in
``proposed_effect["preview"]["grant"]``: ``user_id`` (the login the change
is for, when known), ``group_id`` (the group, when known), ``login_realm``,
and ``platform_admin``. A
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

* The change does not run when it now needs a stricter approval than the
  one it got: the realm became a login realm, the group now gives platform
  admin rights, or MEHO cannot read which case it is.
* A change that acts on a list of objects shown on the card (for example
  the credentials a credential delete removes, registered with
  ``bound_ids``) does not run when the live card lists other ids.
* For an operation registered with :func:`register_harmless_keys_rule`,
  a request whose approved card has no :data:`HARMLESS_KEYS_ONLY` fact
  does not run (:data:`GRANT_REASON_OLD_CARD`): the card was made before
  MEHO decided its approver with that rule. This holds in every realm.

The decision audit row names the login the change is for and says whether
it is the approver (:func:`grant_audit_fields`).

The rights-change mark
----------------------

Every change of rights carries one mark (``ApprovalRequest.rights_change``):
the changes above in MEHO's own login realm, every request of an
operation registered with :func:`register_rights_change_op` (for example
a change of an operation's category or labels), every request whose params
pass the check registered with :func:`register_rights_change_check` (for
example a ``secret.move`` with a Keycloak side), and any other request
that was parked with the mark. :func:`check_rights_change_approval` applies
it: only an admin approves a change of rights
(:meth:`~meho_backplane.auth.operator.Operator.may_approve_rights_change`),
and only with a click in the web console. The command line and REST are
refused with :data:`RIGHTS_CHANGE_CONSOLE_ONLY`. The admin may be the
person who asked; such an approval is recorded as a self-approval. No
other group approves it, at any risk level, also with the break-glass
switch on.

An admin's own call of a registered change of rights in the web console
runs at once, after the same checks of who may make it
(:mod:`~meho_backplane.operations.rights_change_console`).

A person approves each call
---------------------------

Both register functions also mark the operation with
:func:`~meho_backplane.op_classes.declare_per_call_approval`. So a
standing grant, a stored grant row and a run approval never cover a
registered change of rights: making such a grant or such a run approval
list is refused, and each call waits for its own approval. A connector
needs no second mark for this.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.op_classes import declare_per_call_approval
from meho_backplane.operations.approval_sign_in_writes import approval_sign_in_check

if TYPE_CHECKING:
    from meho_backplane.db.models import ApprovalRequest

__all__ = [
    "ASKED_IN_WEB_CONSOLE",
    "FOR_LOGIN",
    "GRANT_REASON_CHANGED",
    "GRANT_REASON_OLD_CARD",
    "GRANT_REASON_STRICTER",
    "GRANT_REASON_UNREADABLE",
    "HARMLESS_KEYS_ONLY",
    "PLATFORM_ADMIN_REASON_DEFAULT",
    "RIGHTS_CHANGE_CONSOLE_ONLY",
    "RIGHTS_CHANGE_REFUSAL",
    "GrantFacts",
    "asked_in_web_console",
    "check_grant_approval",
    "check_rights_change_approval",
    "grant_audit_fields",
    "grant_changed_since_request",
    "grant_facts",
    "is_rights_change",
    "is_rights_change_op",
    "may_make_alone",
    "platform_admin_reason",
    "register_grant_op",
    "register_harmless_keys_rule",
    "register_rights_change_check",
    "register_rights_change_op",
]

_log = structlog.get_logger(__name__)

#: The refusal of a rights-change approval by a person who is not an admin.
RIGHTS_CHANGE_REFUSAL: Final[str] = "This is a change of rights. Only an admin may approve it."

#: The refusal of a rights-change approval on the command line or over REST.
RIGHTS_CHANGE_CONSOLE_ONLY: Final[str] = (
    "This is a change of rights. Approve this change of rights in the web console."
)

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

#: Why a run was refused: the change needs a stricter approval than it got.
GRANT_REASON_STRICTER: Final[str] = (
    "This change now needs a stricter approval than the one it got: the realm or "
    "group it is for changed while the request waited, or MEHO could not read it. "
    "The operation did not run. Send a new request."
)


#: Why a run was refused: the approved card was made before the harmless keys rule.
GRANT_REASON_OLD_CARD: Final[str] = (
    "This request was approved before MEHO changed who must approve this Keycloak "
    "write. The operation did not run. Send a new request."
)

#: The fact on the ``grant`` block that the call sets only harmless keys
#: (:func:`register_harmless_keys_rule`).
HARMLESS_KEYS_ONLY: Final[str] = "harmless_keys_only"

#: The fact on the ``grant`` block that this request acts on the login in
#: ``user_id``, for an operation whose params name no login id (a
#: ``secret.move`` that sets a user's password). Then that login never
#: approves the request, and an unknown login cannot be approved.
FOR_LOGIN: Final[str] = "for_login"

#: The key of ``proposed_effect`` that is ``True`` when a person asked for
#: this change of rights with a click in the web console. The approval
#: queue sets it on every change of rights it parks.
ASKED_IN_WEB_CONSOLE: Final[str] = "asked_in_web_console"


@dataclass(frozen=True)
class _GrantOp:
    #: The params that can hold the id of the login the change is for.
    user_id_params: tuple[str, ...]
    #: The params that can hold the id of the group the change is for.
    group_id_params: tuple[str, ...]
    #: A card key that lists the ``{"id": ...}`` entries the run acts on, and
    #: the refusal when the live card lists other ids. ``None``: no such list.
    bound_ids: tuple[str, str] | None = None
    #: The refusal when the approved card has no list under the ``bound_ids``
    #: key: the request was made before MEHO put that list on the card.
    bound_ids_missing: str | None = None
    #: A check of the params (no I/O): the request is a change of rights only
    #: when it passes. ``None``: every request of the op is one.
    applies: Callable[[dict[str, Any]], bool] | None = None
    #: A person approves each call (:func:`declare_per_call_approval`).
    per_call: bool = True


#: op_id -> where its params carry the login and the group.
_GRANT_OPS: dict[str, _GrantOp] = {}


def register_grant_op(
    op_id: str,
    *,
    user_id_params: tuple[str, ...] = (),
    group_id_params: tuple[str, ...] = (),
    bound_ids: tuple[str, str] | None = None,
    bound_ids_missing: str | None = None,
    applies: Callable[[dict[str, Any]], bool] | None = None,
    per_call: bool = True,
) -> None:
    """Record that *op_id* changes the rights of a login or a group.

    *user_id_params* is empty for a change of a whole group;
    *group_id_params* is empty for a change that names no group.
    *bound_ids* is ``(card key, refusal)`` for a change that acts on a list
    of objects shown on the card: the run is refused with the refusal when
    the live card lists other ids than the approved card.
    *bound_ids_missing* is the refusal when the approved card has no list
    under that key at all: the request is older than the list. *applies*
    checks the params of a request (no I/O): only a request that passes it
    is a change of rights here. A person approves each call of *op_id*
    (:func:`declare_per_call_approval`), unless *per_call* is ``False``.
    """
    _GRANT_OPS[op_id] = _GrantOp(
        user_id_params, group_id_params, bound_ids, bound_ids_missing, applies, per_call
    )
    if per_call:
        declare_per_call_approval((op_id,))


#: Operations whose approval card must carry :data:`HARMLESS_KEYS_ONLY`.
_HARMLESS_KEYS_RULE_OPS: set[str] = set()


def register_harmless_keys_rule(op_id: str) -> None:
    """Record that the ``grant`` block of *op_id* carries :data:`HARMLESS_KEYS_ONLY`.

    The connector sets it on every card it builds. A request whose approved
    card does not have it does not run (:data:`GRANT_REASON_OLD_CARD`).
    """
    _HARMLESS_KEYS_RULE_OPS.add(op_id)


#: Operations that are always a change of rights, but not of one login or
#: group: their requests carry the mark, and nothing else of this module
#: applies to them.
_RIGHTS_CHANGE_OPS: set[str] = set()


def register_rights_change_op(op_id: str) -> None:
    """Record that every request of *op_id* is a change of rights.

    A person approves each call of *op_id* (:func:`declare_per_call_approval`).
    """
    _RIGHTS_CHANGE_OPS.add(op_id)
    declare_per_call_approval((op_id,))


#: Operations whose request is a change of rights when its params pass a
#: check: op_id -> the check. The check reads the params only (no I/O).
_RIGHTS_CHANGE_CHECKS: dict[str, Callable[[dict[str, Any]], bool]] = {}


def register_rights_change_check(op_id: str, check: Callable[[dict[str, Any]], bool]) -> None:
    """Record that a request of *op_id* is a change of rights when *check* passes on its params.

    A person approves each call of *op_id* (:func:`declare_per_call_approval`).
    """
    _RIGHTS_CHANGE_CHECKS[op_id] = check
    declare_per_call_approval((op_id,))


def _params_are_rights_change(request: ApprovalRequest) -> bool:
    check = _RIGHTS_CHANGE_CHECKS.get(request.op_id)
    return check is not None and isinstance(request.params, dict) and check(request.params)


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


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first_text(params: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    return next((v for k in keys if (v := _text(params.get(k)))), None)


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
    if op.applies is not None and not op.applies(params):
        return None
    stamp = _stamp(request)
    return GrantFacts(
        for_login=bool(op.user_id_params) or stamp.get(FOR_LOGIN) is True,
        for_group=bool(op.group_id_params),
        user_id=_first_text(params, op.user_id_params) or _text(stamp.get("user_id")),
        group_id=_first_text(params, op.group_id_params) or _text(stamp.get("group_id")),
        login_realm=stamp.get("login_realm") is not False,
        platform_admin=stamp.get("platform_admin") is not False,
    )


def is_rights_change_op(op_id: str) -> bool:
    """True when *op_id* always counts as a change of rights.

    For a place that knows the operation but not the parked request (a run
    approval list). The login realm is not known there, so every
    registered operation counts. An operation registered with a params
    check (``applies``) does not: only some of its requests are changes of
    rights. Nor does one registered with ``per_call=False``: it is a change
    of rights only in the login realm.
    """
    op = _GRANT_OPS.get(op_id)
    return (op is not None and op.applies is None and op.per_call) or op_id in _RIGHTS_CHANGE_OPS


def is_rights_change(request: ApprovalRequest) -> bool:
    """True when *request* carries the rights-change mark.

    A request whose params pass the check of its operation
    (:func:`register_rights_change_check`) counts too. A request parked
    before the ``rights_change`` column has no stored mark, so a
    registered change in the login realm counts too.
    """
    if (
        request.rights_change
        or request.op_id in _RIGHTS_CHANGE_OPS
        or _params_are_rights_change(request)
    ):
        return True
    facts = grant_facts(request)
    return facts is not None and facts.login_realm


def check_rights_change_approval(operator: Operator, request: ApprovalRequest) -> None:
    """Refuse *operator* when *request* is a change of rights they may not approve.

    Only an admin approves it, and only with a click in the web console
    (:attr:`~meho_backplane.auth.operator.Operator.web_session`). The admin
    may be the person who asked, or a person behind the agent, service or
    runner login that asked. In MEHO's own login realm the person who asked
    approves it only when they asked in the web console
    (:func:`check_grant_approval`).
    """
    if not is_rights_change(request):
        return
    from meho_backplane.operations.approval_queue import RightsChangeApprovalError

    if not operator.may_approve_rights_change():
        reason = RIGHTS_CHANGE_REFUSAL
    elif not operator.web_session:
        reason = RIGHTS_CHANGE_CONSOLE_ONLY
    else:
        return
    raise RightsChangeApprovalError(
        request.id, operator_sub=operator.sub, role=operator.tenant_role.value, reason=reason
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
    if facts.login_realm:
        _check_login_realm_approver(operator, request, facts)


def _check_who_the_change_is_for(
    operator: Operator, request: ApprovalRequest, facts: GrantFacts
) -> None:
    """The login and the group the change is for are known.

    A request that sets the password of a login (:data:`FOR_LOGIN`) is
    never approved by that login.
    """
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
    if _stamp(request).get(FOR_LOGIN) is True and _same_login(facts.user_id, operator.sub):
        raise _refusal(
            request,
            operator,
            "this change sets the password of your own login. A second person must "
            "approve it, and a second login of the same person does not count",
        )


#: Why a platform admin must approve, when the card does not say it.
PLATFORM_ADMIN_REASON_DEFAULT: Final[str] = (
    "It can give platform admin rights in MEHO's own login realm, or MEHO could not "
    "read whether it does"
)


def platform_admin_reason(request: ApprovalRequest) -> str | None:
    """Why *request* can give platform admin rights; ``None`` when it cannot.

    The text comes from the approval card (``platform_admin_reason`` in the
    ``grant`` block), or is :data:`PLATFORM_ADMIN_REASON_DEFAULT` when the
    card does not say it.
    """
    facts = grant_facts(request)
    if facts is None or not facts.login_realm or not facts.platform_admin:
        return None
    return _text(_stamp(request).get("platform_admin_reason")) or PLATFORM_ADMIN_REASON_DEFAULT


def asked_in_web_console(request: ApprovalRequest) -> bool:
    """True when a person asked for *request* with a click in the web console."""
    return (request.proposed_effect or {}).get(ASKED_IN_WEB_CONSOLE) is True


def _check_login_realm_approver(
    operator: Operator, request: ApprovalRequest, facts: GrantFacts
) -> None:
    """In the login realm: a platform admin approves every change of rights.

    The login that asked approves it only when it asked in the web console
    and approves there too, and never when the change can change the
    approval sign-in
    (:func:`~meho_backplane.operations.approval_sign_in_writes.approval_sign_in_check`).
    A person behind the agent, service or runner login that asked may
    approve it, but not a change that can change the approval sign-in.
    """
    if not operator.platform_admin:
        denial = (
            "it changes who has rights in MEHO's own login realm. "
            "A second person who is a platform admin must approve it"
        )
        why = platform_admin_reason(request)
        if why is not None:
            denial = f"{denial}. Why: {why.rstrip('.')}"
        raise _refusal(request, operator, denial)
    own = _same_login(request.principal_sub, operator.sub)
    if own and not (operator.web_session and asked_in_web_console(request)):
        raise _refusal(
            request,
            operator,
            "it changes who has rights in MEHO's own login realm, and you asked for it "
            "with a token, the command line, MCP or an agent. A second person who is a "
            "platform admin must approve it",
        )
    from meho_backplane.operations.people_behind import is_person_behind

    behind = is_person_behind(operator.sub, request.people_behind)
    if (own or behind) and approval_sign_in_check(request):
        raise _refusal(
            request,
            operator,
            "it can change the sign-in that MEHO asks for before an approval. A second "
            "person who is a platform admin must approve it",
        )


def may_make_alone(operator: Operator, request: ApprovalRequest) -> bool:
    """True when *operator* may make the change of rights *request* alone in the web console.

    The same rule of who may as :func:`check_grant_approval`: in MEHO's own
    login realm only a platform admin, and never for a change that can
    change the approval sign-in
    (:func:`~meho_backplane.operations.approval_sign_in_writes.approval_sign_in_check`).
    """
    facts = grant_facts(request)
    if facts is None or not facts.login_realm:
        return True
    return operator.platform_admin and not approval_sign_in_check(request)


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
    group, :data:`GRANT_REASON_OLD_CARD` when the approved card has no
    :data:`HARMLESS_KEYS_ONLY` fact that its operation needs,
    :data:`GRANT_REASON_CHANGED` when a name now names another id,
    :data:`GRANT_REASON_STRICTER` when the change now needs a stricter
    approval, and ``None`` for a request that
    is not a change of rights or for which all of this still holds.
    """
    facts = grant_facts(request)
    if facts is None:
        return None
    log_fields = {
        "approval_request_id": str(request.id),
        "op_id": request.op_id,
        "connector_id": request.connector_id,
    }
    if request.op_id in _HARMLESS_KEYS_RULE_OPS and HARMLESS_KEYS_ONLY not in _stamp(request):
        _log.warning("approval_resume_grant_old_card", **log_fields)
        return GRANT_REASON_OLD_CARD
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
    if not isinstance(live, dict):
        _log.warning("approval_resume_grant_unreadable", **log_fields)
        return GRANT_REASON_UNREADABLE
    reason = (
        _id_moved(facts, live) or _stricter_now(facts, live) or _bound_ids_moved(request, preview)
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


def _card_ids(preview: Any, key: str) -> frozenset[str] | None:
    """The ids of the entries the card lists under *key*; ``None`` when they cannot be read."""
    rows = preview.get(key) if isinstance(preview, dict) else None
    if not isinstance(rows, list):
        return None
    ids = {row["id"] for row in rows if isinstance(row, dict) and _text(row.get("id"))}
    return frozenset(ids) if len(ids) == len(rows) else None


def _bound_ids_moved(request: ApprovalRequest, live_preview: Any) -> str | None:
    """Why the live card lists other objects than the approved card, for a ``bound_ids`` op."""
    op = _GRANT_OPS.get(request.op_id)
    if op is None or op.bound_ids is None:
        return None
    key, reason = op.bound_ids
    approved_preview = (request.proposed_effect or {}).get("preview")
    if op.bound_ids_missing is not None and not (
        isinstance(approved_preview, dict) and key in approved_preview
    ):
        return op.bound_ids_missing
    now = _card_ids(live_preview, key)
    if now is None:
        return GRANT_REASON_UNREADABLE
    approved = _card_ids(approved_preview, key)
    return None if approved == now else reason
