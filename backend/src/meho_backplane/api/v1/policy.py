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
* ``POST /api/v1/policy/explain`` -- why a call runs, waits or is denied,
  and who may approve it (:mod:`meho_backplane.policy.explain`). Runs
  nothing and writes nothing. Asking for yourself (``me``) needs the
  ``operator`` role, like the preview. Asking for login groups or with a
  file that is not imported needs a ``tenant_admin`` or a
  ``platform_admin``. Asking about an approval request (``request_id``)
  needs the right to read approvals. Only a tenant admin or a platform
  admin sees the entries of the policy and their places in it.

Every file route takes ``scope``: ``tenant`` (the caller's own tenant, the
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
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole, is_human_principal
from meho_backplane.auth.rbac import ensure_human_principal, require_approvals_access
from meho_backplane.connectors.policy.apply import POLICY_APPLY_OP_ID, SWITCH_OFF_IMPORT
from meho_backplane.db.engine import get_session
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations.approval_queue import ApprovalNotFoundError
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.policy.document import MAX_POLICY_BYTES
from meho_backplane.policy.explain import (
    FILE_NEEDS_CLASSIFICATION,
    CallQuestion,
    explain_call,
    explain_request,
    without_policy,
)
from meho_backplane.policy.explain_asker import ExplainInputError, Principal, what_if_operator
from meho_backplane.policy.explain_models import PolicyExplainResponse
from meho_backplane.policy.service import (
    KEEP_REVISIONS,
    NO_CHANGE,
    PolicyPlan,
    build_plan,
    export_policy,
    list_history,
)
from meho_backplane.policy.store import InvalidStoredPolicyError, classification_enabled

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/policy", tags=["policy"])

_POLICY_CONNECTOR_ID: Final[str] = "policy-registry-1.x"

PolicyScope = Literal["tenant", "install"]


def _is_policy_admin(operator: Operator) -> bool:
    return operator.platform_admin or operator.tenant_role == TenantRole.TENANT_ADMIN


def _require_policy_admin(operator: Operator = Depends(verify_jwt_and_bind)) -> Operator:
    """``tenant_admin`` or ``platform_admin``; anyone else gets 403 ``insufficient_role``."""
    if _is_policy_admin(operator):
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

    ``diff`` has the sections ``description``, ``taxonomy``,
    ``classifications`` and ``bindings``, each with ``added``, ``changed``
    (``before`` and ``after``) and ``removed``. Each classification entry also says how many
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


# ---------------------------------------------------------------------------
# Explain
# ---------------------------------------------------------------------------


class PolicyExplainBody(BaseModel):
    """The question of ``POST /api/v1/policy/explain``.

    Ask about one call (``connector_id`` and ``op_id``) for yourself
    (``me``) or for a login with ``groups`` (``principal`` says which kind),
    or about a stored approval request (``request_id``) alone.
    """

    model_config = ConfigDict(extra="forbid")

    connector_id: str | None = Field(default=None, min_length=1)
    op_id: str | None = Field(default=None, min_length=1)
    target: str | None = Field(default=None, min_length=1, description="The target's name.")
    me: bool = False
    groups: list[str] = Field(
        default_factory=list, max_length=5, description="Login group kinds, e.g. Operators."
    )
    principal: Principal = "person"
    via: Literal["web", "token"] | None = Field(
        default=None,
        description="For a person: web interface or token. Default: as you call with `me`, "
        "token with `groups`.",
    )
    file: str | None = Field(
        default=None,
        max_length=MAX_POLICY_BYTES,
        description="A policy file (policy: v1) that is not imported; it replaces the "
        "stored policy of `scope`.",
    )
    scope: PolicyScope = "tenant"
    request_id: UUID | None = None


def _unprocessable(text: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=text)


async def _explain_request(
    operator: Operator, body: PolicyExplainBody, request_id: UUID
) -> PolicyExplainResponse:
    other = body.model_dump(exclude={"request_id", "scope"}, exclude_defaults=True)
    if other:
        raise _unprocessable("Ask about a request alone: request_id takes no other field.")
    require_approvals_access(operator)
    try:
        return await explain_request(tenant_id=operator.tenant_id, request_id=request_id)
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="approval_request_not_found"
        ) from exc


def _asker(operator: Operator, body: PolicyExplainBody) -> Operator:
    if body.me == bool(body.groups):
        raise _unprocessable("Ask for yourself (me) or for login groups (groups): one of the two.")
    if not body.me:
        _require_policy_admin(operator)
        return what_if_operator(
            operator, groups=body.groups, principal=body.principal, via=body.via
        )
    if body.principal != "person":
        raise _unprocessable("principal works only with groups.")
    if operator.tenant_role == TenantRole.READ_ONLY:
        # The same role the preview and the call need.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient_role")
    if body.via is None:
        return operator
    if not is_human_principal(operator):
        raise _unprocessable("via works only for a person.")
    return operator.model_copy(update={"web_session": body.via == "web"})


async def _file_document(
    operator: Operator, body: PolicyExplainBody, session: AsyncSession
) -> dict[str, Any] | None:
    if body.file is None:
        return None
    _require_policy_admin(operator)
    _check_scope(operator, body.scope)
    if not classification_enabled():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=FILE_NEEDS_CLASSIFICATION)
    plan = await build_plan(
        session,
        scope=body.scope,
        tenant_id=operator.tenant_id,
        content=body.file.encode("utf-8"),
    )
    # The plan only read; end that transaction before explain opens its own.
    await session.rollback()
    if plan.mistakes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": "The file has mistakes.", "mistakes": plan.mistakes},
        )
    return plan.document


@router.post("/explain", response_model=PolicyExplainResponse)
async def explain(
    body: PolicyExplainBody,
    operator: Annotated[Operator, Depends(verify_jwt_and_bind)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PolicyExplainResponse:
    """Why a call runs, waits or is denied, and who may approve it. Runs nothing.

    The answer is today's answer (the policy gate, decide-only), the policy
    evaluator's answer with every rule it matched (with
    ``POLICY_ENGINE_SHADOW`` on), the classification entries that match, and
    the groups that may approve. No audit row for a call, no approval
    request, no call to an outside system. A caller who is not a tenant
    admin or a platform admin gets the answer without the policy's entries
    (:func:`~meho_backplane.policy.explain.without_policy`).
    """
    _bind_audit("policy.explain", "read")
    answer = await _explain(operator, body, session)
    return answer if _is_policy_admin(operator) else without_policy(answer)


async def _explain(
    operator: Operator, body: PolicyExplainBody, session: AsyncSession
) -> PolicyExplainResponse:
    if body.request_id is not None:
        return await _explain_request(operator, body, body.request_id)
    if body.connector_id is None or body.op_id is None:
        raise _unprocessable("Name the operation: connector_id and op_id.")
    try:
        asker = _asker(operator, body)
        question = CallQuestion(
            asker=asker,
            me=body.me,
            connector_id=body.connector_id,
            op_id=body.op_id,
            groups=tuple(body.groups),
            target=body.target,
            file_document=await _file_document(operator, body, session),
            file_scope=body.scope,
        )
        return await explain_call(question)
    except ExplainInputError as exc:
        raise _unprocessable(str(exc)) from exc
    except InvalidStoredPolicyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
