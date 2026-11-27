# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which step of an approved multi-step operation keeps its own approval.

A person approves a whole multi-step operation (a run). Its steps then go
ahead under that one approval, except the steps this module names: they
wait for their own approval. Two places ask it: the step gate of a
multi-step operation (:func:`~meho_backplane.operations.composite._gate_subop`)
and the child-call rule of the dispatcher (``_dispatch_now``).

With the switch ``CUSTOM_POLICY`` off, a step keeps its own approval when it
is marked ``requires_approval``. Nothing else is asked and nothing more is
read.

With the switch on:

* A step whose operation has only the plain "always ask" mark
  (:func:`~meho_backplane.operations.op_policy.has_plain_mark`) goes ahead
  under the run's approval. An override row of the operation (install-wide
  or of the caller's tenant) that sets the approval flag is a second reason
  to wait, so such a step keeps its own approval.
* Any other ``requires_approval`` step keeps its own approval: for example
  a step an admin's override or a param made wait.
* The fixed rules hold for every step
  (:func:`~meho_backplane.policy.fixed_rules.fixed_rule_for`): a credential
  call, a change of rights or of the policy, a call to an address the
  caller gives, a ``destructive`` step and a delete. A step of a run is
  never a person's own click in the web console, so a change of rights or
  of the policy keeps its own approval whoever approved the run and
  wherever: also an Admin in the web console.
* So do the rules no run approval covers
  (:func:`~meho_backplane.operations.service_grants.never_covered_reason`,
  through ``_never_covered`` of
  :mod:`~meho_backplane.operations.run_approval_gate`, which adds the root
  shell mark). ``fixed_rule_for`` alone has no root-shell rule.
* The step gate asks these rules with the stored facts of the step's
  operation (:func:`step_rule_row`): its parameter schema, HTTP verb, tags,
  class and ``hard_to_undo``.
* In a run that goes on after a person's approval, these rules hold also
  when the step's policy answer lets it run at once
  (:func:`step_waits_after_approval`).

The ``destructive`` level, "always wait" and the credential checks of the
two callers stay where they are; they hold with the switch in both states.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import ColumnElement, or_, select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.policy.fixed_rules import fixed_rule_for
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from uuid import UUID

    from meho_backplane.auth.operator import Operator

__all__ = ["step_keeps_own_approval", "step_rule_row", "step_waits_after_approval"]

_log = structlog.get_logger(__name__)


async def step_keeps_own_approval(descriptor: EndpointDescriptor, caller: Operator) -> bool:
    """True when the approval of the run does not cover this step.

    *descriptor* is the step's row after its policy was applied, *caller*
    the operator the step runs as. See the module text for the rules.
    """
    # Imported here: these modules import the dispatcher, which imports this module.
    from meho_backplane.operations.run_approval_gate import _never_covered

    if not custom_policy_on(get_settings()):
        return descriptor.requires_approval
    if descriptor.requires_approval and not await _only_plain_mark(descriptor, caller):
        return True
    # A step of a run is never the caller's own click in the web console.
    rule = fixed_rule_for(descriptor, caller.model_copy(update={"web_session": False}))
    if rule is not None:
        _log.info("run_step_waits_for_fixed_rule", op_id=descriptor.op_id, rule=f"floor:{rule}")
        return True
    return _never_covered(descriptor)


async def step_waits_after_approval(descriptor: EndpointDescriptor, caller: Operator) -> bool:
    """True when a step that may run at once must wait, in a run after an approval.

    With ``CUSTOM_POLICY`` on, in a run that goes on after a person's
    approval, a step or child call for which :func:`step_keeps_own_approval`
    holds waits for its own approval, also when its policy answer lets it
    run at once. With the switch off, or outside such a run, this is
    ``False`` and nothing is read.
    """
    from meho_backplane.operations.composite import _runs_after_approval

    if not custom_policy_on(get_settings()) or not _runs_after_approval():
        return False
    return await step_keeps_own_approval(descriptor, caller)


async def step_rule_row(step: EndpointDescriptor, tenant_id: UUID | None) -> EndpointDescriptor:
    """The row of a step-gate step that the step rule reads.

    With ``CUSTOM_POLICY`` off this is *step* itself, and nothing is read.
    With the switch on it is a copy of *step* with the stored facts of the
    step's operation, when a row is stored: the parameter schema, the HTTP
    verb, the tags, the class and ``hard_to_undo``. *step* keeps its
    declared values: the gate, the card and the approval request use it.
    """
    if not custom_policy_on(get_settings()):
        return step
    stored = await _stored_step_row(step, tenant_id)
    if stored is None:
        return step
    row = EndpointDescriptor(
        product=step.product,
        version=step.version,
        impl_id=step.impl_id,
        op_id=step.op_id,
        source_kind=step.source_kind,
        safety_level=step.safety_level,
        requires_approval=step.requires_approval,
        parameter_schema=stored.parameter_schema,
        method=stored.method,
        path=stored.path,
        tags=stored.tags,
        op_class=stored.op_class,
        hard_to_undo=stored.hard_to_undo,
    )
    row.policy_always_wait = step.policy_always_wait
    row.credential_call = step.credential_call
    row.code_safety_level = step.code_safety_level
    row.code_requires_approval = step.code_requires_approval
    row.code_parameter_schema = stored.parameter_schema
    return row


async def _stored_step_row(
    step: EndpointDescriptor, tenant_id: UUID | None
) -> EndpointDescriptor | None:
    """The stored row of *step*'s operation; the tenant's own row comes before a built-in one."""
    visible: ColumnElement[bool] = EndpointDescriptor.tenant_id.is_(None)
    if tenant_id is not None:
        visible = or_(visible, EndpointDescriptor.tenant_id == tenant_id)
    stmt = (
        select(EndpointDescriptor)
        .where(
            visible,
            EndpointDescriptor.product == step.product,
            EndpointDescriptor.version == step.version,
            EndpointDescriptor.impl_id == step.impl_id,
            EndpointDescriptor.op_id == step.op_id,
        )
        .order_by(EndpointDescriptor.tenant_id.is_(None))
        .limit(1)
    )
    async with get_sessionmaker()() as session:
        return (await session.execute(stmt)).scalar_one_or_none()


async def _only_plain_mark(descriptor: EndpointDescriptor, caller: Operator) -> bool:
    """True when the plain mark is the only reason *descriptor* asks for approval.

    The applied row does not say whether an override row set the approval
    flag too, so the override rows of the step's operation (install-wide
    and of the caller's tenant) are read again.
    """
    from meho_backplane.operations.op_policy import OpKey, has_plain_mark, load_overrides

    if not has_plain_mark(descriptor):
        return False
    key = OpKey.of(descriptor)
    async with get_sessionmaker()() as session:
        overrides = await load_overrides(session, tenant_id=caller.tenant_id, keys=[key])
    return not any(o.requires_approval for o in overrides.get(key, []))
