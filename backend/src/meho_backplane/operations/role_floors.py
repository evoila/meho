# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The role a person needs for an operation, checked on every path.

A few operations of MEHO's own registries need the ``tenant_admin`` role
from the person who runs them: ``targets.register`` and the five
``topology.*`` writes. Their dedicated REST routes and MCP tools check the
role at the door. ``call_operation``, the REST dispatch route and the CLI
reach the same operation through ``dispatch``, so the role is checked
there as well:

* in the policy gate (``_validate.py``), for a person whose call would
  otherwise run at once or wait for approval. The call is denied and the
  audit row says ``denied``. ``preview_operation`` shows the same verdict.
* at the approval decision (``approval_queue.py``), for the person who
  approves a parked call of an agent or a service login. An approver
  below the floor is refused, the request stays pending, and nothing is
  written. So every resume of an approved call, by the person from an
  approvals surface or by the agent itself, runs a call that a person
  with the role approved. The approve-and-run path (``dispatcher.py``)
  checks the reviewer who runs it from an approvals surface once more.
  A re-run of a failed call by the requester checks the caller's role
  from the caller's token now
  (:mod:`~meho_backplane.operations.approved_run_rights`).

A caller with the Reader role (``read_only``) cannot ask for a change at
all (:func:`reader_change_denial`). This holds for every kind of login,
in a new call and in the requester's own run of an approved request.
The REST, MCP and console doors give the same code and sentence
(:func:`reader_change_refusal`).

The floor applies to people. An agent login parks or is refused by the
policy gate as before, and a service login parks or runs under a grant a
person made.

A connector module registers the floor at import time, next to the
handler (the same shape as ``register_params_check`` and the preview
builders). The floor is in memory; it is not a column of the operation
row.

An operation can also register a requester check
(:func:`register_requester_check`): who may ask for a call with these
params at all, for example only a person. The policy gate runs it first,
for every kind of login, so a refused call is denied before an approval
request or its preview is built.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import TenantRole

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "READER_CHANGE_CODE",
    "READER_CHANGE_SENTENCE",
    "RequesterCheck",
    "asks_for_change",
    "reader_change_denial",
    "reader_change_refusal",
    "register_requester_check",
    "register_role_floor",
    "requester_check_denial",
    "role_floor_denial",
    "role_floor_for",
]

#: The code of the answer to a Reader who asks for a change. REST sends
#: the same code for every role refusal.
READER_CHANGE_CODE: Final[str] = "insufficient_role"

#: The sentence next to :data:`READER_CHANGE_CODE`, the same on every way in.
READER_CHANGE_SENTENCE: Final[str] = (
    "You have the Reader role. A Reader cannot ask for changes, so MEHO did not "
    "store or run this call. Ask an admin for the Operator role if you need to "
    "make changes."
)

#: Why the caller may not ask for a call with these params, or ``None``.
RequesterCheck = Callable[["Operator", dict[str, Any]], str | None]

#: op_id -> the lowest role a person needs. Absent means no floor.
_FLOORS: dict[str, TenantRole] = {}

#: op_id -> who may ask for a call of it. Absent means no check.
_REQUESTER_CHECKS: dict[str, RequesterCheck] = {}

#: The role ladder, lowest first. The same order as ``auth/rbac.py``
#: (REST) and ``mcp/registry.py`` (MCP); those modules are not imported
#: here so the dispatch layer stays free of the HTTP layer.
_ROLE_ORDER: tuple[TenantRole, ...] = (
    TenantRole.READ_ONLY,
    TenantRole.OPERATOR,
    TenantRole.TENANT_ADMIN,
)


def register_role_floor(op_id: str, role: TenantRole) -> None:
    """Record that a person needs at least *role* to run *op_id*."""
    _FLOORS[op_id] = role


def role_floor_for(op_id: str) -> TenantRole | None:
    """The lowest role a person needs for *op_id*, or ``None`` without a floor."""
    return _FLOORS.get(op_id)


def role_floor_denial(op_id: str, role: TenantRole) -> str | None:
    """Why *role* may not run *op_id*, or ``None`` when it may.

    The text names the operation, the role it needs and the caller's role,
    so the caller can tell whom to ask.
    """
    floor = _FLOORS.get(op_id)
    if floor is None or _ROLE_ORDER.index(role) >= _ROLE_ORDER.index(floor):
        return None
    return f"{op_id} needs the {floor.value} role; your role is {role.value}"


def register_requester_check(op_id: str, check: RequesterCheck) -> None:
    """Record *check*: who may ask for a call of *op_id* with given params."""
    _REQUESTER_CHECKS[op_id] = check


def requester_check_denial(op_id: str, operator: Operator, params: dict[str, Any]) -> str | None:
    """Why *operator* may not ask for this call of *op_id*, or ``None`` when it may."""
    check = _REQUESTER_CHECKS.get(op_id)
    return None if check is None else check(operator, params)


def reader_change_refusal(role: TenantRole, *, is_change: bool) -> str | None:
    """The sentence for a Reader who asks for a change, or ``None``.

    *is_change* says whether the call asks for a change. The doors pass
    what they know: the HTTP method of a REST or console route, the class
    of an MCP tool. The policy gate passes :func:`asks_for_change`.
    """
    if role is TenantRole.READ_ONLY and is_change:
        return READER_CHANGE_SENTENCE
    return None


def asks_for_change(descriptor: EndpointDescriptor, params: dict[str, Any] | None) -> bool:
    """True when a call of *descriptor* would not run at once as a plain read.

    That is a call at any risk level above ``safe`` (a write, a delete, a
    multi-step operation that changes something), an operation that always
    waits for approval, a credential write or mint, and a change of rights.
    A ``safe`` read, also a credential read, is not a change.
    """
    from meho_backplane.broadcast.events import resolve_op_class
    from meho_backplane.op_classes import is_credential_capable
    from meho_backplane.operations.grant_approval import is_rights_change_op
    from meho_backplane.operations.rights_change_console import params_write_credential

    op_id = descriptor.op_id
    if descriptor.safety_level != "safe" or descriptor.requires_approval:
        return True
    if getattr(descriptor, "policy_always_wait", False):
        return True
    op_class = resolve_op_class(op_id, getattr(descriptor, "op_class", None))
    if op_class in _CHANGE_CREDENTIAL_CLASSES or is_credential_capable(op_id):
        return True
    return is_rights_change_op(op_id) or params_write_credential(op_id, params)


def reader_change_denial(
    operator: Operator, descriptor: EndpointDescriptor, params: dict[str, Any] | None
) -> str | None:
    """The deny reason when a Reader asks for a change, or ``None``.

    The text starts with :data:`READER_CHANGE_CODE`, so the audit row of
    the refusal names this rule.
    """
    if operator.tenant_role is not TenantRole.READ_ONLY:
        return None
    sentence = reader_change_refusal(
        operator.tenant_role, is_change=asks_for_change(descriptor, params)
    )
    return None if sentence is None else f"{READER_CHANGE_CODE}: {sentence}"


#: The credential classes that change something (a read is not one).
_CHANGE_CREDENTIAL_CLASSES: Final[frozenset[str]] = frozenset(
    {"credential_write", "credential_mint"}
)
