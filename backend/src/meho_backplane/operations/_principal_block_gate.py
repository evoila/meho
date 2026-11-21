# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Refuse an operation call of a login that an admin blocked.

One check for the two places a call runs without a new request:
:func:`~meho_backplane.operations.dispatcher.dispatch` (hosted agent runs,
scheduled runs) and the run of an approved call, where the login that
asked is checked. The list is the one of
:mod:`meho_backplane.auth.principal_block`.

The answers are ``denied`` results: the vendor gets no call. When the
block list cannot be read, the call is refused too (``error``), never let
through.
"""

from __future__ import annotations

import uuid

from meho_backplane.auth.principal_block import (
    PRINCIPAL_BLOCKED_MESSAGE,
    PrincipalBlockUnavailableError,
    is_principal_blocked,
)
from meho_backplane.connectors import OperationResult

__all__ = ["refuse_if_principal_blocked"]

BLOCKED_ERROR_CODE = "principal_blocked"
UNAVAILABLE_ERROR_CODE = "principal_block_check_unavailable"


async def refuse_if_principal_blocked(
    op_id: str, sub: str, tenant_id: uuid.UUID | None
) -> OperationResult | None:
    """Return a refusal when *sub* is blocked, else ``None``."""
    try:
        blocked = await is_principal_blocked(sub, tenant_id)
    except PrincipalBlockUnavailableError:
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
    if not blocked:
        return None
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
