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

A listed step can itself be a multi-step operation with its own list.
The check then goes into that list too, at every level.

Each step is checked with the same :func:`policy_gate` the step gate runs,
with the same declared facts (``dangerous``, no ``requires_approval``,
the level every shipped write step uses), raised by the classification
like at the step gate, and with ``decide_only=True``, so nothing is
written and no grant is used.

With ``AGENT_GRANT_REQUIRED=on`` (#6189) the dispatcher checks the same
list for an agent call before anything runs
(:func:`agent_no_grant_step_reason`): a write step that no grant covers
refuses the whole call with ``agent_no_grant``, so it never stops
half-way.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.deny_reasons import AGENT_NO_GRANT, is_agent_no_grant
from meho_backplane.operations.governed_subops import governed_subops_for
from meho_backplane.operations.op_policy import apply_step_classification

__all__ = ["agent_no_grant_step_reason", "preview_step_verdict"]

_READ_PREFIXES: Final[tuple[str, ...]] = ("GET:", "HEAD:")
_STEP_SAFETY_LEVEL: Final = "dangerous"


def _write_steps(op_id: str) -> list[str]:
    """Every listed write step of *op_id*, at every level, each once.

    A step can itself be a multi-step operation with its own list (for
    example a host evacuation runs a VM migration, which runs a relocate
    step). Its steps are listed too. Each operation is visited once, so a
    list that loops back cannot run forever. Plain ``GET`` / ``HEAD``
    reads are left out.
    """
    steps: list[str] = []
    seen: set[str] = {op_id}
    pending: list[str] = [op_id]
    while pending:
        surface = governed_subops_for(pending.pop(0))
        if surface is None:
            continue
        for step_op_id in surface.sub_op_ids:
            if step_op_id in seen:
                continue
            seen.add(step_op_id)
            # A step with its own list: check its steps too.
            pending.append(step_op_id)
            if not step_op_id.upper().startswith(_READ_PREFIXES):
                steps.append(step_op_id)
    return steps


async def _step_verdict(
    *, operator: Operator, step_op_id: str, connector_id: str, target: Any
) -> tuple[PermissionVerdict, str | None]:
    """The gate's answer for one write step, as the step gate would give it."""
    product, version, impl_id = parse_connector_id(connector_id)
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
    await apply_step_classification(tenant_id=operator.tenant_id, step=step)
    return await policy_gate(
        operator=operator,
        descriptor=step,
        target=target,
        connector_id=connector_id,
        decide_only=True,
    )


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
    names the steps. The steps come from :func:`_write_steps`.
    """
    waiting: list[str] = []
    for step_op_id in _write_steps(op_id):
        verdict, reason = await _step_verdict(
            operator=operator, step_op_id=step_op_id, connector_id=connector_id, target=target
        )
        if verdict is PermissionVerdict.DENY:
            return verdict, f"the step {step_op_id} is denied: {reason or 'denied'}"
        if verdict is PermissionVerdict.NEEDS_APPROVAL:
            waiting.append(step_op_id)
    if not waiting:
        return None
    return PermissionVerdict.NEEDS_APPROVAL, (
        f"these steps have no grant: {', '.join(waiting)}. When the call "
        "runs one of them, that step waits for approval"
    )


async def agent_no_grant_step_reason(
    *,
    operator: Operator,
    op_id: str,
    connector_id: str,
    target: Any,
) -> str | None:
    """The refusal for a write step that no grant of the agent covers, or ``None``.

    With ``AGENT_GRANT_REQUIRED=on`` such a step is refused when the call
    reaches it, so the multi-step operation would stop half-way. The
    dispatcher calls this before it stores an approval request or runs a
    step, with the same step list the preview checks, and refuses the
    whole call instead. The reason starts with ``agent_no_grant:``.
    """
    for step_op_id in _write_steps(op_id):
        verdict, reason = await _step_verdict(
            operator=operator, step_op_id=step_op_id, connector_id=connector_id, target=target
        )
        if verdict is PermissionVerdict.DENY and reason and is_agent_no_grant(reason):
            return (
                f"{AGENT_NO_GRANT}: the operation {op_id!r} has a write step "
                f"{step_op_id} that no grant covers, so MEHO refuses the call "
                f"before any step runs ({reason})"
            )
    return None
