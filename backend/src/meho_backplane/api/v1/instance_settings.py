# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/settings`` -- the settings file: export, import (with preview), history.

The design is ``core.md`` §5; the file, the endpoints and the table are in
``docs/codebase/settings.md``. The work is in
:mod:`meho_backplane.instance_settings.service`; this module maps it to HTTP.

* ``GET /api/v1/settings/export`` -- the file, built from the live state
  (``application/yaml``).
* ``POST /api/v1/settings/import?preview=true`` -- every mistake as one
  sentence plus the diff (added / changed / not in file). Writes nothing and
  calls no outside system.
* ``POST /api/v1/settings/import`` -- add or update, never delete. 422 with
  the same body as the preview when the file has mistakes.
* ``GET /api/v1/settings/history`` -- the revisions, newest first.

Who may call: all four need a login (401 without a token). Export, import and
history need ``tenant_admin`` or ``platform_admin``; every other role gets 403
``insufficient_role``. A ``tenant_admin`` works on their own tenant only; a
file that names another tenant or more than one needs ``platform_admin``. The
import (preview included) is a human-only verb: a machine principal gets 403,
so the revision's ``actor_sub`` is always a real person.

Operator-plane only (REST + the ``meho settings`` CLI from #4048): no MCP tool.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Final
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import authorize_tenant_scope, ensure_human_principal
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import Tenant
from meho_backplane.instance_settings.schema import MAX_FILE_BYTES
from meho_backplane.instance_settings.service import (
    ImportPlan,
    SettingsExportRefusedError,
    SettingsForbiddenError,
    apply_import,
    build_plan,
    dump_settings_yaml,
    export_settings,
    list_history,
)
from meho_backplane.middleware import verify_jwt_and_bind

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

_OP_ID_EXPORT: Final[str] = "settings.export"
_OP_ID_PREVIEW: Final[str] = "settings.import.preview"
_OP_ID_IMPORT: Final[str] = "settings.import"
_OP_ID_HISTORY: Final[str] = "settings.history"


def _require_settings_admin(operator: Operator = Depends(verify_jwt_and_bind)) -> Operator:
    """``tenant_admin`` or ``platform_admin``; anyone else gets 403 ``insufficient_role``."""
    if operator.platform_admin or operator.tenant_role == TenantRole.TENANT_ADMIN:
        return operator
    structlog.get_logger(__name__).warning(
        "settings_insufficient_role",
        operator_sub=operator.sub,
        tenant_role=operator.tenant_role.value,
    )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient_role")


def _require_human_settings_admin(
    operator: Operator = Depends(verify_jwt_and_bind),
) -> Operator:
    """The import's gate: a person first (403 ``human_principal_required``), then the role.

    The human check runs first so a machine principal learns the real reason,
    even though its role is capped below ``tenant_admin`` as well.
    """
    ensure_human_principal(operator)
    return _require_settings_admin(operator)


_settings_admin = Depends(_require_settings_admin)
_human_settings_admin = Depends(_require_human_settings_admin)


class SettingsImportResponse(BaseModel):
    """The preview and the apply answer: plain JSON, the mistakes and the diff.

    ``diff`` has one entry per section (``tenants``, ``policies``,
    ``conventions``, ``targets``), each with ``added``, ``changed`` (``before``
    and ``after``) and ``not_in_file`` lists. It is ``null`` when the file has
    mistakes. On an apply, ``diff`` is what actually changed, ``failed`` lists
    the items that could not be written (the rest were), and ``revision_id`` is
    the new revision (``null`` when nothing changed).
    """

    model_config = ConfigDict(frozen=True)

    preview: bool
    ok: bool
    mistakes: list[str]
    warnings: list[str]
    diff: dict[str, Any] | None
    applied: bool = False
    revision_id: UUID | None = None
    failed: list[str] = []


class SettingsRevision(BaseModel):
    """One ``instance_settings_revision`` row, with its diff worked out on read."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    ts: datetime
    actor_sub: str
    audit_id: UUID
    file_sha256: str
    api_version: str
    exported_from: dict[str, Any] | None
    summary: dict[str, Any]
    diff: dict[str, Any]


def _plan_response(plan: ImportPlan, *, preview: bool) -> SettingsImportResponse:
    return SettingsImportResponse(
        preview=preview,
        ok=not plan.mistakes,
        mistakes=plan.mistakes,
        warnings=plan.warnings,
        diff=plan.diff,
    )


async def _read_file(request: Request) -> bytes:
    content = await request.body()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB.",
        )
    return content


_IMPORT_BODY: Final[dict[str, Any]] = {
    "requestBody": {
        "required": True,
        "description": "The meho-settings.yaml file, as sent (YAML; JSON is valid YAML).",
        "content": {"application/yaml": {"schema": {"type": "string"}}},
    }
}


@router.get(
    "/export",
    response_class=Response,
    # No schema on purpose: the body is a whole YAML file. With ``type: string``
    # oapi-codegen would generate a YAML decode into a Go string, which fails on
    # a mapping and needs gopkg.in/yaml.v2; without a schema the CLI client
    # hands back the raw bytes, which is what saving the file needs.
    responses={200: {"content": {"application/yaml": {}}}},
)
async def export(
    operator: Annotated[Operator, _settings_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant_id: Annotated[UUID | None, Query()] = None,
) -> Response:
    """Build ``meho-settings.yaml`` from the live state.

    A ``tenant_admin`` gets their own tenant. A ``platform_admin`` gets every
    tenant, or the one named by ``tenant_id``. 409 ``settings_export_refused``
    when stored data holds a secret; ``mistakes`` names each path.
    """
    structlog.contextvars.bind_contextvars(audit_op_id=_OP_ID_EXPORT, audit_op_class="read")
    if tenant_id is not None:
        tenant_ids = [authorize_tenant_scope(operator, tenant_id)]
    elif operator.platform_admin:
        tenant_ids = list((await session.execute(select(Tenant.id))).scalars())
    else:
        tenant_ids = [operator.tenant_id]
    try:
        document = await export_settings(session, tenant_ids)
    except SettingsExportRefusedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"kind": "settings_export_refused", "mistakes": exc.mistakes},
        ) from exc
    return Response(
        content=dump_settings_yaml(document),
        media_type="application/yaml",
        headers={"Content-Disposition": 'attachment; filename="meho-settings.yaml"'},
    )


@router.post("/import", response_model=SettingsImportResponse, openapi_extra=_IMPORT_BODY)
async def import_settings(
    request: Request,
    operator: Annotated[Operator, _human_settings_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
    preview: Annotated[bool, Query()] = False,
) -> SettingsImportResponse | JSONResponse:
    """Check the file and either show what it would change or apply it.

    ``?preview=true`` changes nothing. Without it the import adds or updates
    (never deletes) in one transaction, or answers 422 with the mistakes and
    writes nothing.
    """
    content = await _read_file(request)
    try:
        if preview:
            structlog.contextvars.bind_contextvars(
                audit_op_id=_OP_ID_PREVIEW, audit_op_class="read"
            )
            return _plan_response(await build_plan(session, operator, content), preview=True)

        # "read" until the service is about to write (it re-binds "write"), so
        # a refused or unchanged file leaves no write in the audit log.
        structlog.contextvars.bind_contextvars(audit_op_id=_OP_ID_IMPORT, audit_op_class="read")
        result = await apply_import(session, operator, content)
    except SettingsForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    if result.plan.mistakes:
        body = _plan_response(result.plan, preview=False)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=body.model_dump(mode="json"),
        )
    return SettingsImportResponse(
        preview=False,
        ok=not result.failed,
        mistakes=[],
        warnings=result.plan.warnings,
        diff=result.diff,
        applied=result.applied,
        revision_id=result.revision_id,
        failed=result.failed,
    )


@router.get("/history", response_model=list[SettingsRevision])
async def history(
    operator: Annotated[Operator, _settings_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[SettingsRevision]:
    """The settings revisions the caller may see, newest first."""
    structlog.contextvars.bind_contextvars(audit_op_id=_OP_ID_HISTORY, audit_op_class="read")
    rows = await list_history(session, operator, limit=limit)
    return [SettingsRevision.model_validate(row) for row in rows]
