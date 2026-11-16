# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The checks of a call that run after the policy gate, for ``meho policy explain``.

The policy gate is not the last check of a call. Which checks after the
gate a call is asked comes from one function,
:func:`~meho_backplane.operations._after_gate.checks_after_gate`, that the
dispatcher reads too. Explain asks each of them with the same code the call
uses. All only read: no audit row, no approval request, nothing sent to
the vendor.

The other checks after the gate need the call's parameters, the vendor,
or a later step of the call. Explain does not ask them.
:func:`not_checked` names each one that can still refuse the call, with a
fixed name (:data:`NOT_CHECKED_NAMES`).
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.announce_gate import announce_gate_blocks
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.policy.explain_models import Answer

__all__ = [
    "CONNECTOR_RESOLUTION",
    "DESTRUCTIVE_PREVIEW_BINDING",
    "IDEMPOTENCY_KEY",
    "NOT_CHECKED_NAMES",
    "OPERATION_PARAM_CHECK",
    "PERMISSION_PREFLIGHT",
    "not_checked",
    "refusal_after_gate",
]

#: The target's connector is found, and only one (``no_connector``,
#: ``ambiguous_connector``, ``target_required``). Asked when the call runs,
#: also after an approval.
CONNECTOR_RESOLUTION: Final[str] = "connector_resolution"
#: The operation's own check of its parameters before the call waits
#: (``refused_before_approval``).
OPERATION_PARAM_CHECK: Final[str] = "operation_param_check"
#: A ``destructive`` call waits only with the hash of a matching preview
#: and a blast-radius statement (``preview_binding_required``).
DESTRUCTIVE_PREVIEW_BINDING: Final[str] = "destructive_preview_binding"
#: The operation asks the vendor whether its login may do the write
#: (``write_capability_denied``).
PERMISSION_PREFLIGHT: Final[str] = "permission_preflight"
#: An idempotency key that another waiting call holds
#: (``idempotency_key_conflict``).
IDEMPOTENCY_KEY: Final[str] = "idempotency_key"

#: Every name :func:`not_checked` can give, in the order it gives them.
NOT_CHECKED_NAMES: Final[tuple[str, ...]] = (
    OPERATION_PARAM_CHECK,
    DESTRUCTIVE_PREVIEW_BINDING,
    PERMISSION_PREFLIGHT,
    IDEMPOTENCY_KEY,
    CONNECTOR_RESOLUTION,
)

_VERDICT: Final[dict[Answer, PermissionVerdict]] = {
    "run": PermissionVerdict.AUTO_EXECUTE,
    "wait": PermissionVerdict.NEEDS_APPROVAL,
    "deny": PermissionVerdict.DENY,
}


async def refusal_after_gate(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    answer: Answer,
) -> str | None:
    """The reason a check after the policy gate refuses the call, or ``None``.

    *answer* is the policy gate's answer. The checks are the ones the call
    asks for that answer, in the call's order.
    """
    # Imported here: the dispatcher imports the policy modules.
    from meho_backplane.operations._after_gate import AfterGateCheck, checks_after_gate
    from meho_backplane.operations._audit import descriptor_op_class
    from meho_backplane.operations.dispatcher import _ingested_ops_refusal

    for check in checks_after_gate(_VERDICT[answer]):
        if check is AfterGateCheck.ANNOUNCE_RULE:
            block = await announce_gate_blocks(
                operator,
                op_class=descriptor_op_class(descriptor),
                safety_level=descriptor.safety_level,
                target_name=getattr(target, "name", None),
            )
            if block is not None:
                return f"announce_required: {block}"
        elif check is AfterGateCheck.INGESTED_OPS:
            refusal = await _ingested_ops_refusal(descriptor, target, descriptor.op_id, 0.0)
            if refusal is not None:
                return refusal.error
    return None


def not_checked(*, descriptor: EndpointDescriptor, answer: Answer) -> list[str]:
    """The checks after the gate that explain does not ask and that can refuse the call.

    Empty for ``deny``. For ``wait``, only the checks this operation has.
    """
    # Imported here: the dispatcher imports the policy modules.
    from meho_backplane.operations._park_check import park_check_registered
    from meho_backplane.operations._preview import has_permission_preflight

    if answer == "deny":
        return []
    if answer == "run":
        return [CONNECTOR_RESOLUTION]
    names: list[str] = []
    if park_check_registered(descriptor.op_id):
        names.append(OPERATION_PARAM_CHECK)
    if descriptor.safety_level == "destructive":
        names.append(DESTRUCTIVE_PREVIEW_BINDING)
    if has_permission_preflight(descriptor.op_id):
        names.append(PERMISSION_PREFLIGHT)
    names += [IDEMPOTENCY_KEY, CONNECTOR_RESOLUTION]
    return names
