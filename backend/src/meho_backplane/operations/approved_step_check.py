# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check an agent's approved multi-step call again, on every resume path (#6314).

With ``AGENT_GRANT_REQUIRED=on`` the dispatcher checks the write steps of an
agent's multi-step call before the call waits for approval
(:func:`~meho_backplane.operations._preview_steps.agent_step_refusal`). A
grant can change while the call waits, so the steps are checked again when
the approved call runs, before the first step.

An approved call runs in one of two ways:

* **The agent's own run resumes it**: the in-process waiter of a hosted
  agent run, or a re-run by the agent login. The caller is the agent, so
  the check runs for the caller.
* **An approvals surface runs it** (console, CLI, REST ``/approve`` and
  ``/decide``). The caller is the person who approved. The resume binds
  the requester of the request
  (:func:`~meho_backplane.operations.secondary_target_policy.bind_approved_requester`).
  When an agent asked, the check runs for that agent: the agent login, or
  the hosted agent with the person who started the run.

So the result is the same whichever path runs the call. The agent login's
grant name and role come from the record the park stores on the approval
card (:func:`requester_record`). A card with no record (a request made
while the switch was ``off``) cannot be checked. When the operation has
steps to check, the run is refused, and the agent must ask again. A call
with no steps to check runs as before.

Only ``on`` checks again. ``observe`` already logged its "would deny" lines
when the call was made. At resume it only marks them as written, so the
step gate of the resumed call does not log them again: one line per call,
whoever runs it. ``off`` changes nothing.
"""

from __future__ import annotations

from typing import Any

import structlog

from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._preview_steps import (
    agent_acts,
    agent_step_refusal,
    has_steps_to_check,
    mark_step_lines_logged,
)
from meho_backplane.operations.composite import COMPOSITE_DEPTH_TOP_LEVEL, composite_depth_var
from meho_backplane.operations.deny_reasons import POLICY_DENIED
from meho_backplane.operations.secondary_target_policy import (
    approved_agent_requester,
    requester_subject,
)
from meho_backplane.settings import get_settings

__all__ = ["approved_step_refusal", "requester_record"]


def requester_record(operator: Operator) -> dict[str, Any] | None:
    """The requester record a park stores for an agent's call, else ``None``.

    Only when an agent makes the call and ``AGENT_GRANT_REQUIRED`` is not
    ``off``. An approvals surface rebuilds the agent from it to check the
    steps again (:func:`approved_step_refusal`).
    """
    if get_settings().agent_grant_required == "off" or not agent_acts(operator):
        return None
    return requester_subject(operator)


async def approved_step_refusal(
    *, operator: Operator, descriptor: EndpointDescriptor, connector_id: str, target: Any
) -> str | None:
    """The refusal of an approved call's steps for the agent that asked, or ``None``.

    With ``observe`` this refuses nothing. It marks the steps' "would deny"
    lines as written, so the step gate of the resumed call does not write
    them a second time (:func:`mark_step_lines_logged`).
    """
    mode = get_settings().agent_grant_required
    if mode == "off" or composite_depth_var.get() != COMPOSITE_DEPTH_TOP_LEVEL:
        return None
    check = agent_step_refusal if mode == "on" else mark_step_lines_logged
    if agent_acts(operator):
        return await check(
            operator=operator, descriptor=descriptor, connector_id=connector_id, target=target
        )
    requester = approved_agent_requester()
    if requester is None:
        return None
    principal, act = requester
    if principal is None:
        if mode != "on" or not has_steps_to_check(descriptor):
            return None
        return (
            f"{POLICY_DENIED}: the agent's grants for the steps of {descriptor.op_id!r} "
            "cannot be checked: the approval request has no record of the agent. "
            "The agent must ask again."
        )
    with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: act}):
        return await check(
            operator=principal, descriptor=descriptor, connector_id=connector_id, target=target
        )
