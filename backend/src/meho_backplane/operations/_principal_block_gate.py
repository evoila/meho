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
:class:`~meho_backplane.auth.principal_block.PrincipalBlockedStepError`;
:func:`refusal_for_blocked_step` turns it into the same refusal.

The answers are ``denied`` results: the vendor gets no call. When the
block list cannot be read, the call is refused too (``error``), never let
through.
"""

from __future__ import annotations

import uuid

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.principal_block import (
    PRINCIPAL_BLOCKED_MESSAGE,
    PrincipalBlockedStepError,
    PrincipalBlockUnavailableError,
    first_blocked_step_login,
    is_principal_blocked,
)
from meho_backplane.connectors import OperationResult

__all__ = ["refusal_for_blocked_step", "refuse_if_principal_blocked", "refuse_step_if_blocked"]

BLOCKED_ERROR_CODE = "principal_blocked"
UNAVAILABLE_ERROR_CODE = "principal_block_check_unavailable"


def _unavailable(op_id: str) -> OperationResult:
    return OperationResult(
        status="error",
        op_id=op_id,
        error=(
            f"{UNAVAILABLE_ERROR_CODE}: MEHO cannot read the block list, so the "
            "call did not run. Try again."
        ),
        duration_ms=0.0,
        extras={"error_code": UNAVAILABLE_ERROR_CODE, "execution_outcome": "not_started"},
    )


def _blocked(op_id: str) -> OperationResult:
    return OperationResult(
        status="denied",
        op_id=op_id,
        error=f"{BLOCKED_ERROR_CODE}: {PRINCIPAL_BLOCKED_MESSAGE}",
        duration_ms=0.0,
        extras={
            "error_code": BLOCKED_ERROR_CODE,
            "reason": PRINCIPAL_BLOCKED_MESSAGE,
            "execution_outcome": "not_started",
        },
    )


async def refuse_if_principal_blocked(
    op_id: str, sub: str, tenant_id: uuid.UUID | None
) -> OperationResult | None:
    """Return a refusal when *sub* is blocked, else ``None``."""
    try:
        blocked = await is_principal_blocked(sub, tenant_id)
    except PrincipalBlockUnavailableError:
        return _unavailable(op_id)
    return _blocked(op_id) if blocked else None


async def refuse_step_if_blocked(op_id: str, operator: Operator) -> OperationResult | None:
    """Return a refusal when a login of this step is blocked, else ``None``.

    The logins are the step's *operator* and every bound login: the
    operator of the multi-step operation, and on an approved run the
    person who asked.
    """
    try:
        blocked = await first_blocked_step_login(operator.sub, operator.tenant_id)
    except PrincipalBlockUnavailableError:
        return _unavailable(op_id)
    return _blocked(op_id) if blocked is not None else None


def refusal_for_blocked_step(op_id: str, exc: PrincipalBlockedStepError) -> OperationResult:
    """The refusal for a direct request that the HTTP connector base stopped."""
    return _unavailable(op_id) if exc.unavailable else _blocked(op_id)
