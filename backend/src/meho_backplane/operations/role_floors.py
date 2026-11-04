# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The role a person needs for an operation, checked on every path.

A few operations of MEHO's own registries need the ``tenant_admin`` role
from the person who runs them: ``targets.register`` and the five
``topology.*`` writes. Their dedicated REST routes and MCP tools check the
role at the door. ``call_operation``, the REST dispatch route and the CLI
reach the same operation through ``dispatch``, so the role is checked
there as well:

* in the policy gate (``_validate.py``), for a person whose call would
  otherwise run at once or wait for approval. The call is denied and the
  audit row says ``denied``. ``preview_operation`` shows the same verdict.
* at the approval decision (``approval_queue.py``), for the person who
  approves a parked call of an agent or a service login. An approver
  below the floor is refused, the request stays pending, and nothing is
  written. So every resume of an approved call, by the person from an
  approvals surface or by the agent itself, runs a call that a person
  with the role approved. The approve-and-run path (``dispatcher.py``)
  checks the reviewer who runs it from an approvals surface once more.
  The requester's own run of the approved call is not checked again:
  the agent's resume, a hosted agent run, and a re-run of a failed call
  by the requester, also a service login with a lower role.

The floor applies to people. An agent login parks or is refused by the
policy gate as before, and a service login parks or runs under a grant a
person made.

A connector module registers the floor at import time, next to the
handler (the same shape as ``register_params_check`` and the preview
builders). The floor is in memory; it is not a column of the operation
row.
"""

from __future__ import annotations

from meho_backplane.auth.operator import TenantRole

__all__ = ["register_role_floor", "role_floor_denial", "role_floor_for"]

#: op_id -> the lowest role a person needs. Absent means no floor.
_FLOORS: dict[str, TenantRole] = {}

#: The role ladder, lowest first. The same order as ``auth/rbac.py``
#: (REST) and ``mcp/registry.py`` (MCP); those modules are not imported
#: here so the dispatch layer stays free of the HTTP layer.
_ROLE_ORDER: tuple[TenantRole, ...] = (
    TenantRole.READ_ONLY,
    TenantRole.OPERATOR,
    TenantRole.TENANT_ADMIN,
)


def register_role_floor(op_id: str, role: TenantRole) -> None:
    """Record that a person needs at least *role* to run *op_id*."""
    _FLOORS[op_id] = role


def role_floor_for(op_id: str) -> TenantRole | None:
    """The lowest role a person needs for *op_id*, or ``None`` without a floor."""
    return _FLOORS.get(op_id)


def role_floor_denial(op_id: str, role: TenantRole) -> str | None:
    """Why *role* may not run *op_id*, or ``None`` when it may.

    The text names the operation, the role it needs and the caller's role,
    so the caller can tell whom to ask.
    """
    floor = _FLOORS.get(op_id)
    if floor is None or _ROLE_ORDER.index(role) >= _ROLE_ORDER.index(floor):
        return None
    return f"{op_id} needs the {floor.value} role; your role is {role.value}"
