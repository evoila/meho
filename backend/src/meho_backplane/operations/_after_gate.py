# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which checks after the policy gate a call is asked, for each gate answer.

The dispatcher reads this before it asks a check after the gate, and
``meho policy explain`` reads it to ask the same checks
(:mod:`meho_backplane.policy.explain_checks`). So the two cannot drift.

* :attr:`AfterGateCheck.ANNOUNCE_RULE`: the announce rule of a tenant
  (:func:`~meho_backplane.broadcast.announce_gate.announce_gate_blocks`).
  Only for a call that runs at once. A call that waits is not asked again
  after the approval.
* :attr:`AfterGateCheck.INGESTED_OPS`: an ingested operation on a target
  whose connector does not take ingested operations. For a call that runs
  at once (in the connector lookup) and before a call waits.

The checks are listed in the order the call asks them. A denied call is
asked none.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from meho_backplane.db.models import PermissionVerdict

__all__ = ["AfterGateCheck", "checks_after_gate"]


class AfterGateCheck(StrEnum):
    """A check after the policy gate that can refuse the call."""

    ANNOUNCE_RULE = "announce_rule"
    INGESTED_OPS = "ingested_ops"


_ASKED: Final[dict[PermissionVerdict, tuple[AfterGateCheck, ...]]] = {
    PermissionVerdict.AUTO_EXECUTE: (AfterGateCheck.ANNOUNCE_RULE, AfterGateCheck.INGESTED_OPS),
    PermissionVerdict.NEEDS_APPROVAL: (AfterGateCheck.INGESTED_OPS,),
}


def checks_after_gate(verdict: PermissionVerdict) -> tuple[AfterGateCheck, ...]:
    """The checks after the gate the call asks for *verdict*, in order."""
    return _ASKED.get(verdict, ())
