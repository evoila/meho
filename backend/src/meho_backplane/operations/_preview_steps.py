# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The step check of ``preview_operation`` (#5492).

A multi-step operation checks MEHO policy again before each write step
(:func:`~meho_backplane.operations.composite.enforce_subop_policy`). A
grant on the operation alone does not let the steps run: a step without
its own grant waits for approval. So the preview must not say "runs at
once" only because the operation has a grant.

The steps come from the list the connector registers for the operation
(:mod:`~meho_backplane.operations.governed_subops`). That list has every
step the operation may run, reads too, and on some connectors more than
one way to do the work. The preview cannot know which steps the call will
run, because it never contacts the target. So it checks every listed step
that is not a plain ``GET`` / ``HEAD`` read, and the call reads as
covered only when each of them is. That can say "waits" for a call that
then does not need the step; it never says "runs at once" for a call
whose step will wait.

Each step is checked with the same :func:`policy_gate` the step gate runs,
with the same declared facts (``dangerous``, no ``requires_approval``,
the level every shipped write step uses), and with ``decide_only=True``,
so nothing is written and no grant is used.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.governed_subops import governed_subops_for

__all__ = ["preview_step_verdict"]

_READ_PREFIXES: Final[tuple[str, ...]] = ("GET:", "HEAD:")
_STEP_SAFETY_LEVEL: Final = "dangerous"


async def preview_step_verdict(
    *,
    operator: Operator,
    op_id: str,
    connector_id: str,
    target: Any,
) -> tuple[PermissionVerdict, str] | None:
    """The verdict of the operation's write steps, or ``None`` when all may run.

    ``None`` also when the operation lists no steps. A denied step gives
    ``DENY``; else a step that waits gives ``NEEDS_APPROVAL``. The reason
    names the steps.
    """
    surface = governed_subops_for(op_id)
    if surface is None:
        return None
    product, version, impl_id = parse_connector_id(connector_id)
    waiting: list[str] = []
    for step_op_id in surface.sub_op_ids:
        if step_op_id.upper().startswith(_READ_PREFIXES):
            continue
        step = EndpointDescriptor(
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=step_op_id,
            source_kind="composite",
            safety_level=_STEP_SAFETY_LEVEL,
            requires_approval=False,
            parameter_schema={},
        )
        verdict, reason = await policy_gate(
            operator=operator,
            descriptor=step,
            target=target,
            connector_id=connector_id,
            decide_only=True,
        )
        if verdict is PermissionVerdict.DENY:
            return verdict, f"the step {step_op_id} is denied: {reason or 'denied'}"
        if verdict is PermissionVerdict.NEEDS_APPROVAL:
            waiting.append(step_op_id)
    if not waiting:
        return None
    return PermissionVerdict.NEEDS_APPROVAL, (
        f"but these steps have no grant: {', '.join(waiting)}. When the call "
        "runs one of them, that step waits for approval"
    )
