# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The read scope of a service login, checked before a topology refresh.

A topology refresh reads the target through its connector, the same as a
``safe`` operation does. A person can limit the targets a service login
reads (its read scope, :mod:`meho_backplane.operations.service_read_scopes`).
:func:`ensure_refresh_in_read_scope` applies that limit to a refresh, before
MEHO calls the connector. A person, an agent and the scheduler's system login
have no read scope, so the check passes for them.
"""

from __future__ import annotations

from typing import Any

import structlog

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.operations.service_read_scopes import read_scope_denial

__all__ = ["RefreshReadScopeDeniedError", "ensure_refresh_in_read_scope"]

_log = structlog.get_logger(__name__)


class RefreshReadScopeDeniedError(Exception):
    """The target is outside the read scope of the service login.

    ``reason`` starts with the stable error code ``read_scope_denied:`` and
    names how a person changes the scope.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def ensure_refresh_in_read_scope(operator: Operator, target: Any) -> None:
    """Raise :class:`RefreshReadScopeDeniedError` when the scope does not cover *target*.

    Runs only for a service login. Reads one row and writes nothing.
    """
    if operator.principal_kind is not PrincipalKind.SERVICE:
        return
    denial = await read_scope_denial(operator=operator, target=target)
    if denial is None:
        return
    _log.info(
        "topology_refresh_read_scope_denied",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        target_id=str(getattr(target, "id", None)),
    )
    raise RefreshReadScopeDeniedError(denial)
