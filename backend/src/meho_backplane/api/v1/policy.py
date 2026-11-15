# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/policy`` -- the policy file: validate, preview, import, export, history.

The policy file is a file of its own, not a section of the settings file;
see ``docs/codebase/policy.md``. The work is in
:mod:`meho_backplane.policy.service`; this module maps it to HTTP.

* ``POST /api/v1/policy/validate`` -- the mistakes and warnings of a file.
* ``POST /api/v1/policy/import?preview=true`` -- the mistakes and the diff
  (added, changed, removed per section; per classification entry how many
  operations it matches and changes). Writes nothing.
* ``POST /api/v1/policy/import`` -- makes a request that waits until a
  second admin approves it (the ``policy.apply`` operation). Nothing changes
  yet. 202 with the request id; 200 "No change." when the file changes
  nothing; 422 with the mistakes; 409 while the ``POLICY_CLASSIFICATION``
  switch is off.
* ``GET /api/v1/policy/export`` -- the stored policy as a file.
* ``GET /api/v1/policy/history`` -- the imported files, newest first.

Every route takes ``scope``: ``tenant`` (the caller's own tenant, the
default) or ``install`` (the whole install, only a platform admin who is a
person). Who may
call: a ``tenant_admin`` or a ``platform_admin``; every other role gets 403
``insufficient_role``. The import is a human-only verb: a machine
principal gets 403.

Operator-plane only (REST and ``meho policy``): no MCP tool.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole, is_human_principal
from meho_backplane.auth.rbac import ensure_human_principal
from meho_backplane.connectors.policy.apply import POLICY_APPLY_OP_ID, SWITCH_OFF_IMPORT
from meho_backplane.db.engine import get_session
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.policy.document import MAX_POLICY_BYTES
from meho_backplane.policy.service import (
    KEEP_REVISIONS,
    NO_CHANGE,
    PolicyPlan,
    build_plan,
    export_policy,
    list_history,
)
from meho_backplane.policy.store import classification_enabled

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/policy", tags=["policy"])

_POLICY_CONNECTOR_ID: Final[str] = "policy-registry-1.x"

PolicyScope = Literal["tenant", "install"]


def _require_policy_admin(operator: Operator = Depends(verify_jwt_and_bind)) -> Operator:
    """``tenant_admin`` or ``platform_admin``; anyone else gets 403 ``insufficient_role``."""
    if operator.platform_admin or operator.tenant_role == TenantRole.TENANT_ADMIN:
        return operator
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient_role")


def _require_human_policy_admin(operator: Operator = Depends(verify_jwt_and_bind)) -> Operator:
    """The import's gate: a person first (403 ``human_principal_required``), then the role."""
    ensure_human_principal(operator)
    return _require_policy_admin(operator)


_policy_admin = Depends(_require_policy_admin)
_human_policy_admin = Depends(_require_human_policy_admin)


def _check_scope(operator: Operator, scope: PolicyScope) -> None:
    # Like the all-tenant settings export: a platform admin who is a person.
    if scope == "install" and not (operator.platform_admin and is_human_principal(operator)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The install's policy needs a platform admin.",
        )


class PolicyValidateResponse(BaseModel):
    """The mistakes and warnings of a file. ``ok`` is true when it has no mistake."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    scope: PolicyScope
    mistakes: list[str]
    warnings: list[str]


class PolicyImportResponse(BaseModel):
    """The preview and the import answer.

    ``diff`` has the sections ``description``, ``taxonomy`` and
    ``classifications``, each with ``added``, ``changed`` (``before`` and
    ``after``) and ``removed``. Each classification entry also says how many
    operations it ``matches`` and how many of those it ``changes``.
    ``operations_changed`` counts the operations whose values change. ``diff``
    is ``null`` when the file has mistakes. ``based_on`` is the SHA-256 of
    the stored policy the plan was made against. On an import that waits,
    ``approval_request_id`` names the request.
    """

    model_config = ConfigDict(frozen=True)

    preview: bool
    ok: bool
    scope: PolicyScope
    mistakes: list[str]
    warnings: list[str]
    diff: dict[str, Any] | None
    no_change: bool
    file_sha256: str
    based_on: str
    message: str | None = None
    approval_request_id: UUID | None = None


class PolicyRevisionView(BaseModel):
    """One imported policy file."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    ts: datetime
    scope: PolicyScope
    imported_by_sub: str
    approved_by_sub: str
    file_sha256: str
    file: str
    diff: dict[str, Any]


_FILE_BODY: Final[dict[str, Any]] = {
    "requestBody": {
        "required": True,
        "description": "The policy file (policy: v1), as sent (YAML; JSON is valid YAML).",
        "content": {"application/yaml": {"schema": {"type": "string"}}},
    }
}


async def _read_file(request: Request) -> bytes:
    content = await request.body()
    if len(content) > MAX_POLICY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"The file is larger than {MAX_POLICY_BYTES // 1024} KiB.",
        )
    return content


def _plan_response(
    plan: PolicyPlan, *, preview: bool, message: str | None = None
) -> PolicyImportResponse:
    return PolicyImportResponse(
        preview=preview,
        ok=not plan.mistakes,
        scope=plan.scope,
        mistakes=plan.mistakes,
        warnings=plan.warnings,
        diff=plan.diff,
        no_change=plan.no_change,
        file_sha256=plan.file_sha256,
        based_on=plan.based_on,
        message=message,
    )


def _bind_audit(op_id: str, op_class: str) -> None:
    structlog.contextvars.bind_contextvars(audit_op_id=op_id, audit_op_class=op_class)


@router.post("/validate", response_model=PolicyValidateResponse, openapi_extra=_FILE_BODY)
async def validate(
    request: Request,
    operator: Annotated[Operator, _policy_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[PolicyScope, Query()] = "tenant",
) -> PolicyValidateResponse:
    """Check the file: its shape, its categories and levels, and the floors. Writes nothing."""
    _bind_audit("policy.file.validate", "read")
    _check_scope(operator, scope)
    plan = await build_plan(
        session, scope=scope, tenant_id=operator.tenant_id, content=await _read_file(request)
    )
    return PolicyValidateResponse(
        ok=not plan.mistakes, scope=scope, mistakes=plan.mistakes, warnings=plan.warnings
    )


async def _request_import(operator: Operator, plan: PolicyPlan, content: bytes) -> JSONResponse:
    result = await dispatch(
        operator=operator,
        connector_id=_POLICY_CONNECTOR_ID,
        op_id=POLICY_APPLY_OP_ID,
        target=None,
        params={
            "scope": plan.scope,
            "file": content.decode("utf-8"),
            "file_sha256": plan.file_sha256,
            "based_on": plan.based_on,
        },
    )
    if result.status != "awaiting_approval":
        reason = result.error or f"The import request was refused ({result.status})."
        code = status.HTTP_403_FORBIDDEN if result.status == "denied" else status.HTTP_409_CONFLICT
        raise HTTPException(status_code=code, detail=str(reason))
    request_id = result.extras.get("approval_request_id")
    body = _plan_response(
        plan,
        preview=False,
        message="The import waits until a second admin approves it. Nothing changed yet.",
    ).model_copy(update={"approval_request_id": UUID(str(request_id))})
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body.model_dump(mode="json"))


@router.post(
    "/import",
    response_model=PolicyImportResponse,
    responses={202: {"model": PolicyImportResponse}},
    openapi_extra=_FILE_BODY,
)
async def import_policy(
    request: Request,
    operator: Annotated[Operator, _human_policy_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[PolicyScope, Query()] = "tenant",
    preview: Annotated[bool, Query()] = False,
) -> PolicyImportResponse | JSONResponse:
    """Show what the file would change (``?preview=true``), or ask to import it.

    The import replaces the whole policy of *scope*: an entry that is not in
    the file is removed. It waits until a second admin approves it.
    """
    _check_scope(operator, scope)
    content = await _read_file(request)
    if preview:
        _bind_audit("policy.file.preview", "read")
        plan = await build_plan(session, scope=scope, tenant_id=operator.tenant_id, content=content)
        return _plan_response(plan, preview=True, message=NO_CHANGE if plan.no_change else None)

    _bind_audit("policy.file.import", "write")
    if not classification_enabled():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=SWITCH_OFF_IMPORT)
    plan = await build_plan(session, scope=scope, tenant_id=operator.tenant_id, content=content)
    if plan.mistakes:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_plan_response(plan, preview=False).model_dump(mode="json"),
        )
    if plan.no_change:
        return _plan_response(plan, preview=False, message=NO_CHANGE)
    # The plan only read; end that transaction before the dispatcher opens its own.
    await session.rollback()
    return await _request_import(operator, plan, content)


@router.get(
    "/export",
    response_class=Response,
    # No schema: the body is a whole YAML file, and the CLI saves the raw bytes.
    responses={200: {"content": {"application/yaml": {}}}},
)
async def export(
    operator: Annotated[Operator, _policy_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[PolicyScope, Query()] = "tenant",
) -> Response:
    """The stored policy of *scope* as a ``policy: v1`` file."""
    _bind_audit("policy.file.export", "read")
    _check_scope(operator, scope)
    text = await export_policy(session, scope=scope, tenant_id=operator.tenant_id)
    return Response(
        content=text,
        media_type="application/yaml",
        headers={"Content-Disposition": 'attachment; filename="meho-policy.yaml"'},
    )


@router.get("/history", response_model=list[PolicyRevisionView])
async def history(
    operator: Annotated[Operator, _policy_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[PolicyScope, Query()] = "tenant",
    limit: Annotated[int, Query(ge=1, le=KEEP_REVISIONS)] = KEEP_REVISIONS,
) -> list[PolicyRevisionView]:
    """The imported policy files of *scope*, newest first (MEHO keeps the last 20)."""
    _bind_audit("policy.file.history", "read")
    _check_scope(operator, scope)
    rows = await list_history(session, scope=scope, tenant_id=operator.tenant_id, limit=limit)
    return [PolicyRevisionView.model_validate(row) for row in rows]
