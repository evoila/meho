# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Calls that wait for a person because of the params they set.

A connector marks params of an operation with
:func:`~meho_backplane.op_classes.declare_wait_when_set`. A call that sets
one of them waits for a person's approval on every call, whoever makes
it: neither a standing grant nor a run approval covers it. A call without
them keeps the rules of the operation.

With the switch ``CUSTOM_POLICY`` on, a connector's delete check
(:func:`~meho_backplane.op_classes.declare_delete_check`) gives a call
whose params delete something a delete sign of its own. The call waits
for a person, and neither a standing grant nor a run approval covers it.
A call that deletes nothing keeps the rules of the operation. With the
switch off, MEHO does not ask the check.

The dispatcher and the preview both call :func:`raise_descriptor_for_params`
before the policy gate, so the preview reports the wait that the real call
gets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.op_classes import (
    is_credential_capable,
    params_delete_reason,
    params_make_credential_call,
    waits_when_set,
)
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.db.models import EndpointDescriptor

__all__ = ["raise_descriptor_for_params"]


def raise_descriptor_for_params(descriptor: EndpointDescriptor, params: dict[str, Any]) -> None:
    """Make *descriptor* always wait when *params* set a marked param, in place.

    Sets ``requires_approval`` and ``policy_always_wait``. The second one is
    what keeps a standing grant and a run approval from clearing the call.
    *descriptor* is the detached row of one lookup, so the change is never
    written back.

    A call whose params make it a credential call gets ``requires_approval``
    and ``credential_call``. The second one gives it the credential rule:
    no standing grant, no agent grant and no run approval covers it. So
    does every call of an operation a connector marked as credential
    capable, also when its row does not ask for approval (an operation an
    ingest made from a vendor API file): a person's own call waits too.

    With the switch ``CUSTOM_POLICY`` on, a call whose params a delete check
    flags gets ``requires_approval`` and ``delete_call_reason``, the
    sentence of the check. The second one is a delete sign
    (:func:`~meho_backplane.operations.service_grants.delete_grant_reason`).
    """
    if custom_policy_on(get_settings()):
        delete_reason = params_delete_reason(descriptor.op_id, params)
        if delete_reason is not None:
            descriptor.requires_approval = True
            descriptor.delete_call_reason = delete_reason
    if is_credential_capable(descriptor.op_id) or params_make_credential_call(
        descriptor.op_id, params
    ):
        descriptor.requires_approval = True
        descriptor.credential_call = True
    if not waits_when_set(descriptor.op_id, params):
        return
    descriptor.requires_approval = True
    descriptor.policy_always_wait = True
