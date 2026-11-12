# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The person check of the console write pages.

A console session holds the access token of its login. Some console write
pages call a REST handler as a plain function, so the person check of the
REST route does not run there. Those pages call :func:`ensure_ui_human`.
This module imports no route module, so every route package can use it.
"""

from __future__ import annotations

import structlog
from fastapi import HTTPException, status

from meho_backplane.auth.operator import Operator, is_human_principal

__all__ = ["ensure_ui_human"]

_log = structlog.get_logger(__name__)


def ensure_ui_human(operator: Operator) -> None:
    """403 ``human_principal_required`` unless the console login is a person.

    The console session holds the access token of its login. A token of an
    agent, a service or a runner is refused, the same as on the REST
    routes that carry ``require_human_principal``.
    """
    if is_human_principal(operator):
        return
    _log.info(
        "ui_human_principal_required",
        operator_sub=operator.sub,
        principal_kind=operator.principal_kind.value,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="human_principal_required",
    )
