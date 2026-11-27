# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which step of an approved multi-step operation keeps its own approval.

A person approves a whole multi-step operation (a run). Its steps then go
ahead under that one approval, except the steps this module names: they
wait for their own approval. Two places ask it: the step gate of a
multi-step operation
(:func:`~meho_backplane.operations.composite._covered_by_approved_run`) and
the child-call rule of the dispatcher's approved-run branch.

With the switch ``CUSTOM_POLICY`` off, a step keeps its own approval when it
is marked ``requires_approval``, as before.

With the switch on:

* A step whose operation has only the plain "always ask" mark
  (:func:`~meho_backplane.operations.op_policy.has_plain_mark`) goes ahead
  under the run's approval. The owner decided this on 25 Nov 2026.
* Any other ``requires_approval`` step keeps its own approval, as before:
  for example a step an admin's override or a param made wait.
* The fixed rules hold for every step
  (:func:`~meho_backplane.policy.fixed_rules.fixed_rule_for`): a credential
  call, a change of rights or of the policy, a call to an address the
  caller gives, a ``destructive`` step and a delete.
* So do the rules no run approval covers
  (:func:`~meho_backplane.operations.service_grants.never_covered_reason`,
  through ``_never_covered`` of
  :mod:`~meho_backplane.operations.run_approval_gate`, which adds the root
  shell mark). ``fixed_rule_for`` alone has no root-shell rule.

The ``destructive`` level, "always wait" and the credential checks of the
two callers stay where they are; they hold with the switch in both states.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from meho_backplane.policy.fixed_rules import fixed_rule_for
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.db.models import EndpointDescriptor

__all__ = ["step_keeps_own_approval"]

_log = structlog.get_logger(__name__)


def step_keeps_own_approval(descriptor: EndpointDescriptor, caller: Operator) -> bool:
    """True when the approval of the run does not cover this step.

    *descriptor* is the step's row after its policy was applied, *caller*
    the operator the step runs as. See the module text for the rules.
    """
    # Imported here: these modules import the dispatcher, which imports this module.
    from meho_backplane.operations.op_policy import has_plain_mark
    from meho_backplane.operations.run_approval_gate import _never_covered

    if not custom_policy_on(get_settings()):
        return descriptor.requires_approval
    if descriptor.requires_approval and not has_plain_mark(descriptor):
        return True
    rule = fixed_rule_for(descriptor, caller)
    if rule is not None:
        _log.info("run_step_waits_for_fixed_rule", op_id=descriptor.op_id, rule=f"floor:{rule}")
        return True
    return _never_covered(descriptor)
