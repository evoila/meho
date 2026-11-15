# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The login that may only run the conventions drift check.

The tenant wiki's CI job uses it to ask MEHO whether the tenant's operational
conventions still match the team rules. Its token carries the claim
``conventions_drift_only: true`` (:attr:`Operator.conventions_drift_only`).
:func:`reject_conventions_drift_only_elsewhere` runs in
:func:`~meho_backplane.middleware.verify_jwt_and_bind` and keeps such a login
on its one REST route; the MCP seam refuses it on every call.
"""

from __future__ import annotations

from typing import Final

import structlog
from fastapi import HTTPException, Request, status

from meho_backplane.auth.operator import Operator

__all__ = [
    "CONVENTIONS_DRIFT_PATH",
    "CONVENTIONS_DRIFT_ROUTE",
    "SCOPE_VIOLATION",
    "reject_conventions_drift_only_elsewhere",
]

#: The one REST route a ``conventions_drift_only`` login may reach: ``POST``
#: on this path.
CONVENTIONS_DRIFT_PATH: Final[str] = "/api/v1/settings/conventions-drift"
CONVENTIONS_DRIFT_ROUTE: Final[tuple[str, str]] = ("POST", CONVENTIONS_DRIFT_PATH)

#: The 403 detail for such a login anywhere else (REST and MCP).
SCOPE_VIOLATION: Final[str] = "conventions_drift_only_scope_violation"


def reject_conventions_drift_only_elsewhere(operator: Operator, request: Request) -> None:
    """Refuse a ``conventions_drift_only`` login on every route but the drift check.

    Any other method or path gets HTTP 403 ``conventions_drift_only_scope_violation``,
    whatever role the token names. A no-op for every other login.
    """
    if (
        operator.conventions_drift_only
        and (request.method, request.url.path) != CONVENTIONS_DRIFT_ROUTE
    ):
        structlog.get_logger(__name__).warning(
            SCOPE_VIOLATION,
            operator_sub=operator.sub,
            method=request.method,
            path=request.url.path,
            surface="rest",
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SCOPE_VIOLATION)
