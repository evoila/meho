# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Can this caller run one operation now, with no wait? Answered with the call's own steps.

The target form route uses it for the test read of a connector class. It
does not keep a copy of the rule. It asks the same two things a real call
asks:

1. The call's row lookup
   (:func:`~meho_backplane.operations._lookup.lookup_descriptor`). It knows
   the tenant's own on/off setting of a built-in operation, the tenant's
   own rows of a connector, and the final risk level and approval flag
   (stored values, overrides, the classification rules when that switch is
   on, and the floors). No row means the call is refused.
2. The call's policy gate
   (:func:`~meho_backplane.operations._request_preview.governance_and_gate`),
   asked with ``decide_only`` so it writes nothing: no audit row, no
   approval request, no grant use. This is the gate the preview and
   ``meho policy explain`` use too.

The answer holds a fixed code and a fixed sentence. It never holds a stored
value: no risk level text from a row, no override reason, no tenant id, and
no reason string from the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import PermissionVerdict
from meho_backplane.operations._lookup import _render_connector_id, lookup_descriptor
from meho_backplane.operations._request_preview import governance_and_gate

__all__ = [
    "NOT_ALLOWED",
    "NOT_ENABLED",
    "RUN_NOW_SENTENCES",
    "WAITS_FOR_APPROVAL",
    "RunNow",
    "run_now",
]

#: The read is turned off for the caller's tenant, or the tenant's own rows of
#: the connector do not have it. A call is refused.
NOT_ENABLED: Final = "not_enabled"
#: A call of the read waits for a person to approve it.
WAITS_FOR_APPROVAL: Final = "waits_for_approval"
#: The caller may not run the read.
NOT_ALLOWED: Final = "not_allowed"

#: The one sentence of each code. Fixed text; never built from stored values.
RUN_NOW_SENTENCES: Final[dict[str, str]] = {
    NOT_ENABLED: "This test read is not turned on for your tenant, so MEHO refuses a call of it.",
    WAITS_FOR_APPROVAL: "A call of this test read waits until a person approves it.",
    NOT_ALLOWED: "Your login is not allowed to run this test read.",
}


@dataclass(frozen=True)
class RunNow:
    """Whether the caller can run the read now. ``code`` is ``None`` when it can."""

    can_run: bool
    code: str | None = None

    @property
    def sentence(self) -> str | None:
        """The fixed sentence of :attr:`code`, or ``None`` when the read can run."""
        return None if self.code is None else RUN_NOW_SENTENCES[self.code]


async def run_now(
    operator: Operator, *, product: str, version: str, impl_id: str, op_id: str
) -> RunNow:
    """Ask the call's own lookup and policy whether *operator* can run *op_id* now."""
    descriptor = await lookup_descriptor(
        tenant_id=operator.tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
    )
    connector_id = _render_connector_id(product, version, impl_id)
    if descriptor is None or connector_id is None:
        return RunNow(False, NOT_ENABLED)
    fields, _gate = await governance_and_gate(
        operator=operator,
        descriptor=descriptor,
        target=None,
        connector_id=connector_id,
        params={},
    )
    # ``policy_verdict`` is the final answer: the gate, then the step checks.
    verdict = PermissionVerdict(fields["policy_verdict"])
    if verdict is PermissionVerdict.AUTO_EXECUTE:
        return RunNow(True)
    if verdict is PermissionVerdict.NEEDS_APPROVAL:
        return RunNow(False, WAITS_FOR_APPROVAL)
    return RunNow(False, NOT_ALLOWED)
