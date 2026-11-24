# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Refuse an operation call of a login that an admin blocked.

One check for the places a call runs without a new request:
:func:`~meho_backplane.operations.dispatcher.dispatch` (hosted agent runs,
scheduled runs), each step of a multi-step operation, and the run of an
approved call, where the login that asked is checked. The list is the one
of :mod:`meho_backplane.auth.principal_block`.

A step checks its operator and every login bound with
:func:`~meho_backplane.auth.principal_block.checking_logins_for_steps`
(:func:`refuse_step_if_blocked`). A request that a step sends straight to
the target is stopped by the HTTP connector base, which raises
:class:`~meho_backplane.auth.principal_block.PrincipalBlockedStepError`. A
refused step or request stops the whole operation, and
:func:`refusal_for_blocked_step` builds its answer.

The answers are ``denied`` results: the vendor gets no call. When the
block list cannot be read, the call is refused too (``error``), never let
through.

When nothing was sent yet, the answer says ``not_started``. When an earlier
step or request of the same operation was sent
(:mod:`meho_backplane.auth.principal_block_run`), the answer is
``uncertain``: a part of the operation may have run.
"""

from __future__ import annotations

import uuid
from typing import Final

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.principal_block import (
    PRINCIPAL_BLOCKED_MESSAGE,
    PrincipalBlockedStepError,
    PrincipalBlockUnavailableError,
    is_principal_blocked,
    refusal_of_step,
)
from meho_backplane.auth.principal_block_run import request_was_sent
from meho_backplane.connectors import OperationResult
from meho_backplane.operations.uncertain_outcome import UNCERTAIN_ADVICE, UNCERTAIN_OUTCOME

__all__ = ["refusal_for_blocked_step", "refuse_if_principal_blocked", "refuse_step_if_blocked"]

BLOCKED_ERROR_CODE = "principal_blocked"
UNAVAILABLE_ERROR_CODE = "principal_block_check_unavailable"


def _after_earlier_request(text: str) -> str:
    return f"{text} A part of the operation may have run. {UNCERTAIN_ADVICE}"


_UNCERTAIN_EXTRAS: Final[dict[str, object]] = {
    "execution_outcome": UNCERTAIN_OUTCOME,
    "retry": False,
    "remediation": UNCERTAIN_ADVICE,
}


def _unavailable(op_id: str, *, earlier_sent: bool = False) -> OperationResult:
    if earlier_sent:
        text = _after_earlier_request(
            "MEHO cannot read the block list, so it stopped the operation."
        )
    else:
        text = "MEHO cannot read the block list, so the call did not run. Try again."
    outcome = _UNCERTAIN_EXTRAS if earlier_sent else {"execution_outcome": "not_started"}
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"{UNAVAILABLE_ERROR_CODE}: {text}",
        duration_ms=0.0,
        extras={"error_code": UNAVAILABLE_ERROR_CODE, **outcome},
    )


def _blocked(op_id: str, *, earlier_sent: bool = False) -> OperationResult:
    text = PRINCIPAL_BLOCKED_MESSAGE
    outcome = _UNCERTAIN_EXTRAS if earlier_sent else {"execution_outcome": "not_started"}
    return OperationResult(
        status="denied",
        op_id=op_id,
        error=f"{BLOCKED_ERROR_CODE}: {_after_earlier_request(text) if earlier_sent else text}",
        duration_ms=0.0,
        extras={"error_code": BLOCKED_ERROR_CODE, "reason": PRINCIPAL_BLOCKED_MESSAGE, **outcome},
    )


async def refuse_if_principal_blocked(
    op_id: str, sub: str, tenant_id: uuid.UUID | None, *, earlier_sent: bool = False
) -> OperationResult | None:
    """Return a refusal when *sub* is blocked, else ``None``.

    *earlier_sent* is true when an earlier step of the same operation was
    sent, also in the run before an approval.
    """
    try:
        blocked = await is_principal_blocked(sub, tenant_id)
    except PrincipalBlockUnavailableError:
        return _unavailable(op_id, earlier_sent=earlier_sent)
    return _blocked(op_id, earlier_sent=earlier_sent) if blocked else None


async def refuse_step_if_blocked(op_id: str, operator: Operator) -> OperationResult | None:
    """Return a refusal when this step must not be sent, else ``None``.

    The step is refused when an earlier step or request of the run was
    refused, when one of its logins is blocked, or when the list cannot be
    read (:func:`~meho_backplane.auth.principal_block.refusal_of_step`). The
    logins are the step's *operator* and every bound login: the operator of
    the multi-step operation, and on an approved run the person who asked.
    The refusal is noted for the multi-step operation, which then stops with
    it.
    """
    refusal = await refusal_of_step(operator)
    return None if refusal is None else refusal_for_blocked_step(op_id, refusal)


def refusal_for_blocked_step(op_id: str, exc: PrincipalBlockedStepError) -> OperationResult:
    """The answer of an operation that stopped because a step or request was refused."""
    earlier_sent = request_was_sent()
    if exc.unavailable:
        return _unavailable(op_id, earlier_sent=earlier_sent)
    return _blocked(op_id, earlier_sent=earlier_sent)
