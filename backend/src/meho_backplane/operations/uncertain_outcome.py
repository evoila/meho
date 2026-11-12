# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "uncertain" outcome: MEHO does not know if a change happened.

An approved change can end in a way MEHO cannot judge. The vendor system
may have done the work, and MEHO lost the answer. Three cases:

* The request was sent, then the answer never came (a read timeout or a
  dropped connection) on an operation that changes something.
* The change ran, but the audit record could not be saved.
* MEHO stopped in the middle of an approved run (a restart), so no result
  was stored.

In all three, every view shows ``uncertain`` with one piece of advice: do
not run it again, check the target first. MEHO itself never runs such a
change again (``retry`` is ``False``, and an approved request can start only
once).

This module holds the words and the two small rules every path shares, so
the views, the dispatcher and the restart recovery say the same thing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import httpx

from meho_backplane.connectors import OperationResult

__all__ = [
    "UNCERTAIN_ADVICE",
    "UNCERTAIN_OUTCOME",
    "changes_target",
    "envelope_is_uncertain",
    "request_may_have_reached_vendor",
    "result_interrupted_by_restart",
]

#: Value of ``extras["execution_outcome"]`` when MEHO does not know.
UNCERTAIN_OUTCOME: Final[str] = "uncertain"

#: The one piece of advice every view shows for an uncertain run.
UNCERTAIN_ADVICE: Final[str] = (
    "MEHO does not know if the change happened. Do not run it again. Check the target first."
)

#: Transport faults after which the vendor may already have the request: the
#: answer did not come, or the connection dropped while or after MEHO wrote
#: it. A connect timeout, a pool timeout and a refused connection are not
#: here: nothing was sent.
_MAY_HAVE_SENT: Final[tuple[type[httpx.TransportError], ...]] = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


def changes_target(safety_level: str | None) -> bool:
    """Whether an operation with this risk level may change the target.

    Only ``safe`` is a read. An unknown level counts as a change, so a doubt
    ends as ``uncertain`` and never as a plain error.
    """
    return safety_level != "safe"


def request_may_have_reached_vendor(exc: httpx.TransportError) -> bool:
    """Whether the vendor may already hold the request that raised *exc*."""
    return isinstance(exc, _MAY_HAVE_SENT)


def envelope_is_uncertain(envelope: Mapping[str, Any] | None) -> bool:
    """Whether a stored result says MEHO does not know what happened."""
    if not envelope or envelope.get("status") == "ok":
        return False
    extras = envelope.get("extras")
    return isinstance(extras, Mapping) and extras.get("execution_outcome") == UNCERTAIN_OUTCOME


def result_interrupted_by_restart(op_id: str) -> OperationResult:
    """The result stored for an approved run that MEHO lost in a restart."""
    return OperationResult(
        status="error",
        op_id=op_id,
        duration_ms=0.0,
        error=(
            "interrupted_by_restart: MEHO stopped while this approved operation was "
            "running. Do not run it again. Check the target first."
        ),
        extras={
            "error_code": "interrupted_by_restart",
            "execution_outcome": UNCERTAIN_OUTCOME,
            "retry": False,
            "remediation": UNCERTAIN_ADVICE,
        },
    )
