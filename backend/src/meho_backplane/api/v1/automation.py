# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/automation`` — the paired automation surface (Task #3029).

The REST twin of the ``meho_automation_list`` MCP meta-tool and the
``meho automation list`` CLI verb — three fronts on one dispatch answer
(Initiative #2900's paired-surface activation). All three read the shared
:func:`~meho_backplane.operations.addon_automation.active_automation_surface`
projection, so REST / CLI / MCP give the same verdict for the same tenant.

Gating follows the established static-surface / server-side-gate discipline
(#2109): the route is always registered (so the CLI's generated client and the
OpenAPI snapshot are deterministic regardless of pairing state), and activation
is enforced *inside* the handler. When no paired, contract-healthy add-on
advertises the ``automation`` meta-tool family, the surface is inactive and the
route answers ``403`` — the same "not available while unpaired" verdict the MCP
call-time gate returns — rather than leaking an empty page that reads as "paired
but empty". Tenant scope comes from the JWT-validated operator, never the query
string.
"""

from __future__ import annotations

from typing import Final

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.operations.addon_automation import (
    AutomationSurfaceResponse,
    active_automation_surface,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/automation", tags=["automation"])

#: Module-level Depends closure — avoid ruff B008. Read-only floor: discovering
#: whether governed automation is available is benign, mirroring the
#: ``meho_automation_list`` tool's ``read_only`` role.
_require_read = Depends(require_role(TenantRole.READ_ONLY))

#: Canonical audit op_id — the ``meho.automation.*`` family the MCP tool and CLI
#: verb bind too, so a ``query_audit`` filter catches the read transport-independently.
_LIST_OP_ID: Final[str] = "meho.automation.list"


@router.get("", response_model=AutomationSurfaceResponse)
async def list_automation_surface(
    operator: Operator = _require_read,
) -> AutomationSurfaceResponse:
    """Return the paired automation add-on(s) and their advertised surface.

    ``403`` (``automation_addon_not_active``) when the automation family is
    inactive — nothing paired, or every candidate pairing is
    contract-incompatible — so an unpaired backplane has no live automation
    surface here, matching the MCP tool's true-absence gate. Otherwise returns
    the non-empty provider list.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_LIST_OP_ID,
        audit_op_class="read",
    )
    surface = await active_automation_surface(operator.tenant_id)
    if not surface.providers:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="automation_addon_not_active",
        )
    return surface
