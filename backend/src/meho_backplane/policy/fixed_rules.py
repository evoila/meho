# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The fixed rules: rules in the code that no policy and no admin can lower.

No policy file, no tenant admin and no platform admin can lower a fixed
rule (``docs/decisions/2026-11-25-custom-policy-model.md``, decision 5).
Each rule has a short id and one plain sentence (:data:`FIXED_RULES`).

:func:`fixed_rule_for` names the fixed rule that holds for one call. It
does not decide the call by itself: today's checks still decide whether
the call runs, waits or is refused. It says that nothing after those
checks (a standing grant, a run approval, a later policy rule) may let the
call run where those checks make it wait. A later step can make a call
stricter, never looser.

The function wraps today's checks and does not copy them:

* a change of the policy or of rights:
  :func:`~meho_backplane.operations.grant_approval.is_rights_change_op`;
* a credential call:
  :func:`~meho_backplane.operations.service_grants.descriptor_credential_reason`;
* an address the caller gives:
  :func:`~meho_backplane.operations.service_grants.caller_address_grant_reason`;
* the ``destructive`` level, on the row or in the code
  (``code_safety_level``), so a lowered level still counts;
* a delete sign:
  :func:`~meho_backplane.operations.service_grants.delete_grant_reason`;
* with the switch ``CUSTOM_POLICY`` on, a change of rights in a target
  system (the field ``changes_rights`` of the operation).

These are the kinds of :func:`~meho_backplane.operations.service_grants.never_covered_reason`
that are fixed rules. Per-call approval and "always wait" stay their own
rules there; they are not fixed rules. Nor is the role floor
(:mod:`meho_backplane.operations.role_floors`).

Two fixed rules are never the answer of :func:`fixed_rule_for`:
``agent_never_approves`` is a rule of the approve path, and
``root_shell_waits`` (the root shell mark, :func:`root_shell_rule`) lets a
standing grant made for the operation cover a call; only a run approval
never covers it.

Sandbox mode keeps its delete exception (``docs/decisions/sandbox-mode.md``):
a delete grant may still cover a ``destructive`` or delete call of a
service login. The grant lookup checks that case.

Pure functions; no database. The checks of ``operations`` are imported
inside the functions, because the call gate imports this module.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import is_human_principal
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Mapping

    from meho_backplane.auth.operator import Operator
    from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "AGENT_NEVER_APPROVES",
    "CALLER_ADDRESS_WAITS",
    "CREDENTIAL_WAITS",
    "DELETE_WAITS",
    "DESTRUCTIVE_WAITS",
    "FIXED_RULES",
    "POLICY_CHANGE_OPS",
    "POLICY_CHANGE_WAITS",
    "RIGHTS_CHANGE_WAITS",
    "ROOT_SHELL_WAITS",
    "TARGET_RIGHTS_WAITS",
    "FixedRuleId",
    "fixed_rule_for",
    "fixed_rule_of_operation",
    "root_shell_rule",
]

FixedRuleId = Literal[
    "credential_waits",
    "rights_change_waits",
    "policy_change_waits",
    "agent_never_approves",
    "destructive_waits",
    "delete_waits",
    "caller_address_waits",
    "root_shell_waits",
    "target_rights_waits",
]

CREDENTIAL_WAITS: Final = "credential_waits"
RIGHTS_CHANGE_WAITS: Final = "rights_change_waits"
POLICY_CHANGE_WAITS: Final = "policy_change_waits"
AGENT_NEVER_APPROVES: Final = "agent_never_approves"
DESTRUCTIVE_WAITS: Final = "destructive_waits"
DELETE_WAITS: Final = "delete_waits"
CALLER_ADDRESS_WAITS: Final = "caller_address_waits"
ROOT_SHELL_WAITS: Final = "root_shell_waits"
TARGET_RIGHTS_WAITS: Final = "target_rights_waits"

#: Each fixed rule, with one plain sentence.
FIXED_RULES: Final[Mapping[FixedRuleId, str]] = MappingProxyType(
    {
        CREDENTIAL_WAITS: "A call that reads, writes or makes a credential waits for a person.",
        RIGHTS_CHANGE_WAITS: (
            "A change of rights waits for a person, except an Admin's own change in the web "
            "console after a fresh sign-in (a tenant Admin outside MEHO's own login realm, or a "
            "platform admin) that is not destructive and writes no credential."
        ),
        POLICY_CHANGE_WAITS: (
            "A change of the policy waits for a person, except an Admin's own change in the "
            "web console after a fresh sign-in."
        ),
        AGENT_NEVER_APPROVES: "An agent, a service login and a runner never approve.",
        DESTRUCTIVE_WAITS: "A destructive operation always waits for approval.",
        DELETE_WAITS: (
            "A delete waits for approval, except an Admin's own change of rights in the web "
            "console that is not destructive, after a fresh sign-in (a tenant Admin outside "
            "MEHO's own login realm, or a platform admin); in sandbox mode a delete grant may "
            "cover a delete."
        ),
        CALLER_ADDRESS_WAITS: (
            "A call to an address that the caller gives always waits for approval."
        ),
        ROOT_SHELL_WAITS: (
            "A command that runs as root on a host always waits for approval. No run "
            "approval covers it; a standing grant made for the operation may."
        ),
        TARGET_RIGHTS_WAITS: (
            "With the switch CUSTOM_POLICY on, a change of rights in a target system "
            "waits for approval, and no standing grant and no run approval covers it."
        ),
    }
)

#: The operations that change the policy. They are changes of rights too.
POLICY_CHANGE_OPS: Final[frozenset[str]] = frozenset(
    {"policy.apply", "policy.classify", "policy.op_edit", "policy.op_remove_override"}
)


def _own_console_click(caller: Operator) -> bool:
    """True for a person's own click in the web console, not a hosted agent run."""
    return caller.web_session and is_human_principal(caller) and resolve_actor_sub() is None


def _rights_rule(op_id: str, caller: Operator) -> FixedRuleId | None:
    """The rule for a change of the policy or of rights, or ``None``.

    A person's own click in the web console gets ``None``: there the
    console check (:mod:`meho_backplane.operations.rights_change_console`)
    lets only an Admin's own change run at once, and every other change
    waits by its level, as today.
    """
    # Imported here: the operations package imports the call gate, which imports this module.
    from meho_backplane.operations.grant_approval import is_rights_change_op

    if not is_rights_change_op(op_id) or _own_console_click(caller):
        return None
    return POLICY_CHANGE_WAITS if op_id in POLICY_CHANGE_OPS else RIGHTS_CHANGE_WAITS


def _operation_rule(descriptor: EndpointDescriptor) -> FixedRuleId | None:
    """The rule that holds for every caller of *descriptor*, without the change of rights."""
    from meho_backplane.operations.service_grants import (
        caller_address_grant_reason,
        delete_grant_reason,
        descriptor_credential_reason,
    )

    settings = get_settings()
    if custom_policy_on(settings) and getattr(descriptor, "changes_rights", None):
        return TARGET_RIGHTS_WAITS
    if descriptor_credential_reason(descriptor) is not None:
        return CREDENTIAL_WAITS
    if caller_address_grant_reason(descriptor) is not None:
        return CALLER_ADDRESS_WAITS
    if "destructive" in (descriptor.safety_level, getattr(descriptor, "code_safety_level", None)):
        return DESTRUCTIVE_WAITS
    patterns = settings.service_grant_delete_shaped_patterns
    if delete_grant_reason(descriptor.op_id, patterns, descriptor) is not None:
        return DELETE_WAITS
    return None


def fixed_rule_of_operation(descriptor: EndpointDescriptor) -> FixedRuleId | None:
    """The fixed rule that holds for *descriptor* for some caller, or ``None``.

    The edit checks of an operation's policy ask this: they do not know who
    will call. It is :func:`fixed_rule_for` without the one exception for a
    caller (a person's own click in the web console on a change of rights),
    and with the root shell mark (:func:`root_shell_rule`).
    """
    from meho_backplane.operations.grant_approval import is_rights_change_op

    op_id = descriptor.op_id
    if is_rights_change_op(op_id):
        return POLICY_CHANGE_WAITS if op_id in POLICY_CHANGE_OPS else RIGHTS_CHANGE_WAITS
    return _operation_rule(descriptor) or root_shell_rule(op_id)


def root_shell_rule(op_id: str) -> FixedRuleId | None:
    """``root_shell_waits`` when a connector marked *op_id* as a command run as root, else ``None``.

    The mark is :func:`~meho_backplane.op_classes.declare_root_shell`. No
    run approval covers such a call.
    """
    from meho_backplane.op_classes import is_root_shell

    return ROOT_SHELL_WAITS if is_root_shell(op_id) else None


def fixed_rule_for(descriptor: EndpointDescriptor, caller: Operator) -> FixedRuleId | None:
    """The fixed rule that holds when *caller* calls *descriptor*, or ``None``.

    When more than one rule holds, the first of this order is named: a
    change of the policy, a change of rights, with the switch
    ``CUSTOM_POLICY`` on a change of rights in a target system, a
    credential, an address the caller gives, ``destructive`` (on the row or
    in the code), a delete. Every kind of caller gets the
    same rule, except a person's own click in the web console on a change
    of rights or of the policy (:func:`_rights_rule`).

    The answer never makes a call wait by itself. It says that no grant, no
    run approval and no later policy rule may let the call run where
    today's checks make it wait. ``agent_never_approves`` and
    ``root_shell_waits`` are never the answer here (see the module text).
    """
    rights = _rights_rule(descriptor.op_id, caller)
    if rights is not None:
        return rights
    return _operation_rule(descriptor)
