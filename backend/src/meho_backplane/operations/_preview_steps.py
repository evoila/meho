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
(:func:`agent_step_refusal`): a write step that no grant covers
refuses the whole call with ``agent_no_grant``, and a write step that a
``deny`` grant meets refuses it with ``agent_grant_denied`` (#6285). A
multi-step operation that changes something and declares no steps is
refused with ``agent_steps_not_declared`` (#6298). The dispatcher runs the
check again when an approved call resumes. So the call never stops
half-way.
"""

from __future__ import annotations

from typing import Any, Final

import structlog

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.auth.permissions import agent_grant_name
from meho_backplane.broadcast.history import WRITE_OP_CLASSES
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.operations._audit import descriptor_op_class
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.deny_reasons import (
    AGENT_GRANT_DENIED,
    AGENT_NO_GRANT,
    AGENT_STEPS_NOT_DECLARED,
    deny_reason_code,
    is_agent_no_grant,
)
from meho_backplane.operations.governed_subops import governed_subops_for, steps_declared
from meho_backplane.operations.op_policy import apply_step_classification
from meho_backplane.settings import get_settings

__all__ = ["agent_acts", "agent_step_refusal", "has_steps_to_check", "preview_step_verdict"]

_log = structlog.get_logger(__name__)

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


def agent_acts(operator: Operator) -> bool:
    """True when an agent makes this call: an agent login, or a hosted agent run."""
    return operator.principal_kind is PrincipalKind.AGENT or resolve_actor_sub() is not None


def _agent_label(operator: Operator) -> str:
    """The agent a grant is for: the run's agent, or ``agent:<name>`` of the login."""
    return resolve_actor_sub() or agent_grant_name(operator)


def _how_to_change_grant(agent: str) -> str:
    return (
        f"An admin changes the grant with `meho agent grant list --principal {agent}` "
        "and `meho agent grant revoke <grant-id>`, or in the console under Agents."
    )


def _undeclared_write_composite(descriptor: EndpointDescriptor) -> bool:
    """True for a multi-step operation that changes something and lists no steps."""
    return (
        descriptor.source_kind == "composite"
        and descriptor_op_class(descriptor) in WRITE_OP_CLASSES
        and not steps_declared(descriptor.op_id)
    )


def has_steps_to_check(descriptor: EndpointDescriptor) -> bool:
    """True when :func:`agent_step_refusal` has something to check for this operation.

    That is a listed write step, or a multi-step write that lists no steps.
    """
    return bool(_write_steps(descriptor.op_id)) or _undeclared_write_composite(descriptor)


def _steps_not_declared_refusal(operator: Operator, descriptor: EndpointDescriptor) -> str | None:
    """Refuse an agent's call of a multi-step write that lists no steps (#6298).

    MEHO cannot check the agent's grants for steps it does not know, so the
    call could stop half-way at a step the grants refuse. ``on`` refuses
    the call; ``observe`` logs one line and lets it go on.
    """
    agent = _agent_label(operator)
    if get_settings().agent_grant_required != "on":
        _log.info(
            "agent_steps_not_declared_would_deny",
            agent=agent,
            tenant_id=str(operator.tenant_id),
            op_id=descriptor.op_id,
        )
        return None
    return (
        f"{AGENT_STEPS_NOT_DECLARED}: agent {agent} may not run the multi-step "
        f"operation {descriptor.op_id!r}: it changes something, but its connector "
        "does not list its steps, so MEHO cannot check the agent's grants for each "
        "step and refuses the call before it starts. The connector must declare "
        "the steps of the operation (register_governed_subops), or declare that it "
        "has none (declare_no_governed_subops)."
    )


async def agent_step_refusal(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    connector_id: str,
    target: Any,
) -> str | None:
    """The refusal for a write step the agent's grants refuse, or ``None``.

    With ``AGENT_GRANT_REQUIRED=on`` a write step is refused when the call
    reaches it if no grant covers it, or if a ``deny`` grant meets it
    (#6285). The multi-step operation would then stop half-way. The
    dispatcher calls this before it stores an approval request or runs a
    step, and again when an approved call resumes (#6298), with the same
    step list the preview checks, and refuses the whole call instead. A
    step with no grant gives a reason that starts with ``agent_no_grant:``.
    A step with a ``deny`` grant gives a reason that names the agent, the
    step and the grant's own text, so its code is ``agent_grant_denied``.

    A multi-step operation that changes something and declares no steps
    is refused for an agent with ``agent_steps_not_declared:`` (#6298).

    With ``observe`` only the ``agent_no_grant_would_deny`` and
    ``agent_steps_not_declared_would_deny`` lines are written and this
    returns ``None``: a ``deny`` grant on a step is left to the step gate,
    as before.
    """
    op_id = descriptor.op_id
    if agent_acts(operator) and _undeclared_write_composite(descriptor):
        return _steps_not_declared_refusal(operator, descriptor)
    refuse_denied_steps = get_settings().agent_grant_required == "on"
    for step_op_id in _write_steps(op_id):
        verdict, reason = await _step_verdict(
            operator=operator, step_op_id=step_op_id, connector_id=connector_id, target=target
        )
        if verdict is not PermissionVerdict.DENY or not reason:
            continue
        if is_agent_no_grant(reason):
            return (
                f"{AGENT_NO_GRANT}: the operation {op_id!r} has a write step "
                f"{step_op_id} that no grant covers, so MEHO refuses the call "
                f"before any step runs ({reason})"
            )
        if refuse_denied_steps and deny_reason_code(reason) == AGENT_GRANT_DENIED:
            agent = _agent_label(operator)
            return (
                f"agent {agent} may not run the operation {op_id!r}: it has a write "
                f"step {step_op_id} that a grant denies, so MEHO refuses the call "
                f"before any step runs ({reason}). {_how_to_change_grant(agent)}"
            )
    return None
