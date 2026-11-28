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
* A step whose level the policy raised (an override row or a
  classification entry, above the code's own level) goes ahead under the
  run's approval only when the person who approved the run may approve
  that level (:func:`_raised_past_the_approver`). An Operator may not
  approve another person's ``dangerous`` change, so such a step waits for
  its own approval when an Operator approved the run.
* The step gate asks these rules with the stored facts of the step's
  operation (:func:`step_rule_rows`): its parameter schema, HTTP verb, tags,
  class and risk level. The risk level of such a copy is the higher of the
  step's declared level and the stored one, so a stored ``destructive``
  level makes the step keep its own approval. When both a built-in row and
  a row of the caller's tenant are stored, a fact counts when either row
  holds it.
  The copies also carry the stored ``hard_to_undo``. With the switch on
  it is a delete sign, so a step whose stored row has it keeps its own
  approval, as a child call of the same operation does. The re-check of a
  step's own approval reads the stored levels too
  (:func:`~meho_backplane.operations.approved_run_rights.approved_step_refusal_now`).
* In a run that goes on after a person's approval, these rules hold also
  when the step's policy answer lets it run at once
  (:func:`step_wait_reason_after_approval`). The reason MEHO stores for
  such a wait names the rule, for example ``floor:rights_change_waits``.
  A credential read that the run sent at once before the wait is sent
  again with no new wait (the dispatcher checks that before it asks).

The ``destructive`` level, "always wait" and the credential checks of the
two callers stay where they are; they hold with the switch in both states.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog
from sqlalchemy import ColumnElement, or_, select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.policy.fixed_rules import fixed_rule_for
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from meho_backplane.auth.operator import Operator

__all__ = [
    "WAIT_REASON_PREFIX",
    "step_keeps_own_approval",
    "step_rule_rows",
    "step_wait_reason_after_approval",
]

_log = structlog.get_logger(__name__)

_LEVEL_RANK: Final[dict[str, int]] = {"safe": 0, "caution": 1, "dangerous": 2, "destructive": 3}

#: The start of the reason MEHO stores when the step rule makes a step wait
#: (the ``wait_reason`` of the step's ``approval.request`` audit row).
WAIT_REASON_PREFIX = (
    "run_step_keeps_own_approval: a step of a run after an approval keeps its own approval"
)


async def step_keeps_own_approval(descriptor: EndpointDescriptor, caller: Operator) -> bool:
    """True when the approval of the run does not cover this step.

    *descriptor* is the step's row after its policy was applied, *caller*
    the operator the step runs as. See the module text for the rules.
    """
    if not custom_policy_on(get_settings()):
        return descriptor.requires_approval
    return await _own_approval_reason(descriptor, caller) is not None


async def step_wait_reason_after_approval(
    rows: Sequence[EndpointDescriptor], caller: Operator
) -> str | None:
    """Why a step that may run at once must wait, in a run after an approval.

    With ``CUSTOM_POLICY`` on, in a run that goes on after a person's
    approval, a step or child call for which :func:`step_keeps_own_approval`
    holds on one of *rows* waits for its own approval, also when its policy
    answer lets it run at once. The answer is the reason MEHO stores for the
    wait: :data:`WAIT_REASON_PREFIX` and why, for example
    ``floor:rights_change_waits``. ``None``: no such wait. With the switch
    off, or outside such a run, the answer is ``None`` and nothing is read.
    """
    from meho_backplane.operations.composite import _runs_after_approval

    if not custom_policy_on(get_settings()) or not _runs_after_approval():
        return None
    for row in rows:
        why = await _own_approval_reason(row, caller)
        if why is not None:
            return f"{WAIT_REASON_PREFIX} ({why})"
    return None


async def _own_approval_reason(descriptor: EndpointDescriptor, caller: Operator) -> str | None:
    """The rule by which *descriptor* keeps its own approval, with the switch on; else ``None``.

    A fixed rule is named first (``floor:<rule id>``), then the approval
    flag, then a rule no run approval covers, then a level the policy
    raised past what the run's approver may approve.
    """
    # Imported here: these modules import the dispatcher, which imports this module.
    from meho_backplane.operations.run_approval_gate import _never_covered

    # A step of a run is never the caller's own click in the web console.
    rule = fixed_rule_for(descriptor, caller.model_copy(update={"web_session": False}))
    if rule is not None:
        _log.info("run_step_waits_for_fixed_rule", op_id=descriptor.op_id, rule=f"floor:{rule}")
        return f"floor:{rule}"
    if descriptor.requires_approval and not await _only_plain_mark(descriptor, caller):
        return "requires_approval"
    if _never_covered(descriptor):
        return "no_run_approval_covers_it"
    if await _raised_past_the_approver(descriptor):
        return "approver_may_not_approve_raised_level"
    return None


async def _raised_past_the_approver(descriptor: EndpointDescriptor) -> bool:
    """True when the policy raised *descriptor*'s level to one the run's approver may not approve.

    The policy (an override row or a classification entry) raised the level
    when it is higher than the code's own level. The person who approved
    the run is read off the decision of the approval that the run goes on
    under (:func:`~meho_backplane.operations.approved_run_rights.run_approver_may_approve`).
    """
    from meho_backplane.operations.approved_run_rights import run_approver_may_approve

    level = descriptor.safety_level
    code_level = descriptor.code_safety_level or level
    if _level_rank(level) <= _level_rank(code_level):
        return False
    if await run_approver_may_approve(level):
        return False
    _log.info("run_step_waits_for_raised_level", op_id=descriptor.op_id, level=level)
    return True


async def step_rule_rows(
    step: EndpointDescriptor, tenant_id: UUID | None
) -> list[EndpointDescriptor]:
    """The rows of a step-gate step that the step rule reads.

    With ``CUSTOM_POLICY`` off this is ``[step]``, and nothing is read. With
    the switch on it holds one copy of *step* for each stored row of the
    step's operation (the built-in row and the row of the caller's tenant),
    with the stored facts of that row: the parameter schema, the HTTP verb,
    the tags and the class. Its risk level is the higher of the step's
    declared level and the stored one. A fact counts when either row holds
    it: the step keeps its own approval when the rule holds for one copy.
    The copies also carry the stored ``hard_to_undo``: with the switch on it
    is a delete sign, so a step whose stored row has it keeps its own approval.
    With no stored row it is ``[step]``. *step* keeps its declared values:
    the gate, the card and the approval request use it.
    """
    if not custom_policy_on(get_settings()):
        return [step]
    stored = await _stored_step_rows(step, tenant_id)
    return [_with_stored_facts(step, row) for row in stored] or [step]


def _with_stored_facts(step: EndpointDescriptor, stored: EndpointDescriptor) -> EndpointDescriptor:
    """A copy of *step* with the stored facts of *stored*, at the higher of the two levels."""
    row = EndpointDescriptor(
        product=step.product,
        version=step.version,
        impl_id=step.impl_id,
        op_id=step.op_id,
        source_kind=step.source_kind,
        safety_level=max(step.safety_level, stored.safety_level, key=_level_rank),
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


def _level_rank(level: str) -> int:
    """The rank of a risk level; an unknown level counts as ``safe``."""
    return _LEVEL_RANK.get(level, 0)


async def _stored_step_rows(
    step: EndpointDescriptor, tenant_id: UUID | None
) -> list[EndpointDescriptor]:
    """The stored rows of *step*'s operation: the built-in row and the caller's tenant's row."""
    visible: ColumnElement[bool] = EndpointDescriptor.tenant_id.is_(None)
    if tenant_id is not None:
        visible = or_(visible, EndpointDescriptor.tenant_id == tenant_id)
    stmt = select(EndpointDescriptor).where(
        visible,
        EndpointDescriptor.product == step.product,
        EndpointDescriptor.version == step.version,
        EndpointDescriptor.impl_id == step.impl_id,
        EndpointDescriptor.op_id == step.op_id,
    )
    async with get_sessionmaker()() as session:
        return list((await session.execute(stmt)).scalars())


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
