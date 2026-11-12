# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The rights of the session store and the lowest role that has each one.

Each right is separate, so a route checks exactly the right it needs. The
rights map to the tenant roles of the login (``read_only`` < ``operator``
< ``tenant_admin``):

* ``upload`` -- every person of the tenant may upload their own sessions.
* ``list`` and ``read`` -- ``operator`` and up. A person without them
  cannot see that a session exists.
* ``delete``, ``read_quarantine`` and ``set_retention`` -- ``tenant_admin``
  only.

Only a person may use the store: an agent, service or runner login gets
``403`` (``require_human_principal``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from meho_backplane.auth.operator import Operator, TenantRole

__all__ = ["RIGHT_FLOORS", "SessionRight", "has_right"]


class SessionRight(StrEnum):
    """One right of the session store."""

    UPLOAD = "upload"
    LIST = "list"
    READ = "read"
    DELETE = "delete"
    READ_QUARANTINE = "read_quarantine"
    SET_RETENTION = "set_retention"


_ORDER: Final[tuple[TenantRole, ...]] = (
    TenantRole.READ_ONLY,
    TenantRole.OPERATOR,
    TenantRole.TENANT_ADMIN,
)

#: The lowest tenant role that has each right.
RIGHT_FLOORS: Final[dict[SessionRight, TenantRole]] = {
    SessionRight.UPLOAD: TenantRole.READ_ONLY,
    SessionRight.LIST: TenantRole.OPERATOR,
    SessionRight.READ: TenantRole.OPERATOR,
    SessionRight.DELETE: TenantRole.TENANT_ADMIN,
    SessionRight.READ_QUARANTINE: TenantRole.TENANT_ADMIN,
    SessionRight.SET_RETENTION: TenantRole.TENANT_ADMIN,
}


def has_right(operator: Operator, right: SessionRight) -> bool:
    """Whether *operator* has *right* in its current tenant."""
    return _ORDER.index(operator.tenant_role) >= _ORDER.index(RIGHT_FLOORS[right])
