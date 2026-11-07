# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/checks/dashboard-files/*`` -- move a Dashboard and its Sensors as a file.

* ``GET /api/v1/checks/dashboard-files/export?name=<name>`` -- the file of
  one Dashboard: its settings and its Sensors, with target names as
  placeholders. Role: ``operator``. An unknown name is 404
  ``dashboard_not_found``.
* ``POST /api/v1/checks/dashboard-files/import`` -- create or update the
  Dashboard and its Sensors from a file, with a target map. Role:
  ``tenant_admin``. A file that cannot be imported is 422 with one entry per
  problem (unmapped or unknown target, unknown or unsafe operation, a
  connector that does not fit its target). ``dry_run`` says what would
  change and writes nothing. The audit row of an import names the Dashboard,
  the target map and what changed.

The rules of the import are in :mod:`meho_backplane.checks.dashboard_file`.
"""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status

from meho_backplane.audit_cutover import note_preview
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import authorize_tenant_scope, require_role
from meho_backplane.checks.dashboard_file import (
    DashboardImportConflictError,
    DashboardImportRefusedError,
    export_dashboard,
    import_dashboard,
)
from meho_backplane.checks.dashboard_file_schemas import (
    DashboardFile,
    DashboardImportRequest,
    DashboardImportResult,
)
from meho_backplane.checks.dashboard_schemas import _NAME_MAX_LENGTH
from meho_backplane.checks.service import SensorOperationNotFoundError

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/checks/dashboard-files", tags=["checks-dashboards"])

_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

_OP_IDS: Final[dict[str, str]] = {
    "export": "dashboard.export",
    "import": "dashboard.import",
}


def _bind_tenant_scope(operator: Operator, target_tenant: UUID) -> None:
    structlog.contextvars.bind_contextvars(
        audit_tenant_scope=("other" if target_tenant != operator.tenant_id else "self"),
    )


@router.get("/export", response_model=DashboardFile)
async def export_dashboard_file(
    operator: Annotated[Operator, _require_operator],
    name: str = Query(min_length=1, max_length=_NAME_MAX_LENGTH),
    tenant_filter: UUID | None = Query(default=None),
) -> DashboardFile:
    """Return the file of the Dashboard *name*: its settings and its Sensors.

    Target names become placeholders. The file has no ids, no state and no
    mail address.
    """
    target_tenant = authorize_tenant_scope(operator, tenant_filter)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["export"],
        audit_op_class="read",
        audit_dashboard_name=name,
    )
    _bind_tenant_scope(operator, target_tenant)
    exported = await export_dashboard(target_tenant, name)
    if exported is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="dashboard_not_found",
        )
    return exported


@router.post("/import", response_model=DashboardImportResult)
async def import_dashboard_file(
    body: DashboardImportRequest,
    operator: Annotated[Operator, _require_admin],
) -> DashboardImportResult:
    """Create or update a Dashboard and its Sensors from a file.

    Every target placeholder needs an entry in ``target_map``. The file is
    checked as a whole first; a problem anywhere refuses the whole file with
    422 and a list of every problem. Sensors and the Dashboard are matched by
    name, so the same file imported twice changes nothing the second time.
    """
    target_tenant = authorize_tenant_scope(operator, body.tenant_id)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["import"],
        audit_op_class="read" if body.dry_run else "write",
        audit_dashboard_name=body.file.dashboard.name,
        audit_dry_run=body.dry_run,
        audit_target_map=dict(body.target_map),
    )
    _bind_tenant_scope(operator, target_tenant)
    if body.dry_run:
        note_preview()
    try:
        result = await import_dashboard(target_tenant, operator.sub, body)
    except DashboardImportRefusedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[{"loc": list(p.loc), "msg": p.message, "type": p.kind} for p in exc.problems],
        ) from exc
    except SensorOperationNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[{"loc": ["body", "file"], "msg": str(exc), "type": exc.error_code}],
        ) from exc
    except DashboardImportConflictError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=exc.error_code,
        ) from exc
    structlog.contextvars.bind_contextvars(
        audit_dashboard_id=None if result.dashboard_id is None else str(result.dashboard_id),
        audit_import={
            "changed": result.changed,
            "dashboard": result.dashboard_action,
            "sensors": {s.name: s.action for s in result.sensors},
            "members_added": result.members_added,
            "members_removed": result.members_removed,
        },
    )
    return result
