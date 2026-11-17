# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Calls that wait for a person because of the params they set.

A connector marks params of an operation with
:func:`~meho_backplane.op_classes.declare_wait_when_set`. A call that sets
one of them waits for a person's approval on every call, whoever makes
it: neither a standing grant nor a run approval covers it. A call without
them keeps the rules of the operation.

The dispatcher and the preview both call :func:`raise_descriptor_for_params`
before the policy gate, so the preview reports the wait that the real call
gets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.op_classes import waits_when_set

if TYPE_CHECKING:
    from meho_backplane.db.models import EndpointDescriptor

__all__ = ["raise_descriptor_for_params"]


def raise_descriptor_for_params(descriptor: EndpointDescriptor, params: dict[str, Any]) -> None:
    """Make *descriptor* always wait when *params* set a marked param, in place.

    Sets ``requires_approval`` and ``policy_always_wait``. The second one is
    what keeps a standing grant and a run approval from clearing the call.
    *descriptor* is the detached row of one lookup, so the change is never
    written back.
    """
    if not waits_when_set(descriptor.op_id, params):
        return
    descriptor.requires_approval = True
    descriptor.policy_always_wait = True
