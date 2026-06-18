# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant-admin lift for the agent-grants console surface.

Initiative #1824 (G10.8 Agents console), Task #1832 (T5). The grants
surface differs from the rest of the agents console in one load-bearing
way: **even reading grants is tenant_admin** (the listing reveals the
tenant's least-privilege posture, which is governance data --
:mod:`meho_backplane.api.v1.agent_grants` gates every route, including
the list / show reads, at ``tenant_admin``). So unlike the agent
definitions surface -- where an ``operator`` can read and only writes
are admin-gated -- the entire grants surface (list / show / create /
elevate / revoke) is tenant_admin.

This module reuses the shared operator lift the rest of the agents
console already ships (:mod:`meho_backplane.ui.routes.agents.operator`):
it loads the encrypted BFF session row, decrypts the access token, and
re-validates it through the chassis JWT chain to recover the operator's
:class:`~meho_backplane.auth.operator.TenantRole`. The only difference
is the gate's failure detail (``agent_grants_require_tenant_admin``) and
that it is wired onto the **read** routes too.

One dependency:

* :func:`resolve_grants_admin_or_403` -- the all-paths gate (list /
  show / create / elevate / revoke). Returns the full
  :class:`~meho_backplane.auth.operator.Operator` after asserting
  :class:`~meho_backplane.auth.operator.TenantRole.TENANT_ADMIN`; a
  non-admin caller raises 403 so a crafted request cannot bypass it.

The soft :func:`~meho_backplane.ui.routes.agents.operator.resolve_role_probe`
is re-exported for the templates' affordance hints, but on this surface
``is_tenant_admin`` is always ``True`` by the time a render runs (the
hard gate already passed), so the probe is only used to keep the
template-context shape consistent with the rest of the console.
"""

from __future__ import annotations

import structlog
from fastapi import HTTPException, Request, status

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.agents.operator import (
    OperatorRoleProbe,
    _lift_operator,
    resolve_role_probe,
)

__all__ = [
    "OperatorRoleProbe",
    "resolve_grants_admin_or_403",
    "resolve_role_probe",
]

_log = structlog.get_logger(__name__)


async def resolve_grants_admin_or_403(
    request: Request,
    session_ctx: UISessionContext | None = None,
) -> Operator:
    """FastAPI dependency: lift the operator + gate the whole surface to tenant_admin.

    Used by **every** grants route -- the read paths (``GET
    /ui/agents/grants`` list + ``GET /ui/agents/grants/{grant_id}``
    show) as well as the writes (create / elevate / revoke). A non-admin
    call raises 403 with a structured detail so the HTMX response carries
    the gate-failure semantics back to the surface, mirroring the REST
    surface's ``require_role(TenantRole.TENANT_ADMIN)`` posture on the
    same routes (``api/v1/agent_grants.py``).
    """
    if session_ctx is None:
        session_ctx = await require_ui_session(request)
    operator = await _lift_operator(session_ctx)
    if operator.tenant_role != TenantRole.TENANT_ADMIN:
        _log.info(
            "ui_agent_grants_non_admin_denied",
            session_id=str(session_ctx.session_id),
            operator_sub=session_ctx.operator_sub,
            tenant_role=operator.tenant_role.value,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent_grants_require_tenant_admin",
        )
    return operator
