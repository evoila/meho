# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The checks of a call that run after the policy gate, for ``meho policy explain``.

The policy gate is not the last check of a call. Two more checks can still
refuse a call that the gate lets run or wait:

* **The announce rule of a tenant**
  (:func:`~meho_backplane.broadcast.announce_gate.announce_gate_blocks`).
  It runs only when the gate lets the call run at once. A call that waits
  for approval is not asked again after the approval.
* **An ingested operation on a target whose connector does not take
  ingested operations** (``ingested_ops_not_accepted``,
  :func:`~meho_backplane.operations.dispatcher._ingested_ops_refusal`).
  It runs for a call that runs at once and for a call that would wait.

Explain asks both with the same code the call uses. Both only read: no
audit row, no approval request, nothing sent to the vendor.

One check stays out: the operation's own check before the wait for
approval (:mod:`~meho_backplane.operations._park_check`). It needs the
call's parameters, and explain asks about a call without parameters.
:data:`NOT_CHECKED` says so in the answer.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.announce_gate import announce_gate_blocks
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.policy.explain_models import Answer

__all__ = ["NOT_CHECKED", "refusal_after_gate"]

#: The one line of the answer that names what explain does not check.
NOT_CHECKED: Final[str] = (
    "Not checked: the operation's own check of its parameters. "
    "It runs only with the parameters of a real call."
)


async def refusal_after_gate(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    answer: Answer,
) -> str | None:
    """The reason a check after the policy gate refuses the call, or ``None``.

    *answer* is the policy gate's answer. A call the gate denies is not
    asked again. The checks run in the order of the call: the announce
    rule first (only for ``run``), then the ingested operation check.
    """
    # Imported here: the dispatcher imports the policy modules.
    from meho_backplane.operations._audit import descriptor_op_class
    from meho_backplane.operations.dispatcher import _ingested_ops_refusal

    if answer == "deny":
        return None
    if answer == "run":
        block = await announce_gate_blocks(
            operator,
            op_class=descriptor_op_class(descriptor),
            safety_level=descriptor.safety_level,
            target_name=getattr(target, "name", None),
        )
        if block is not None:
            return f"announce_required: {block}"
    refusal = await _ingested_ops_refusal(descriptor, target, descriptor.op_id, 0.0)
    return refusal.error if refusal is not None else None
