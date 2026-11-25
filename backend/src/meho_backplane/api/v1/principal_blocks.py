# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/principals/blocked*`` -- the admin block list.

An admin blocks a person by their login id (``sub``). MEHO then refuses
every request of that login within a few seconds, on every process
(:mod:`meho_backplane.auth.principal_block`).

Routes (REST and CLI only; there is no MCP tool):

* ``GET /api/v1/principals/blocked`` -- the block list. A platform admin
  sees every block, a tenant admin sees the blocks of their tenant.
* ``POST /api/v1/principals/blocked`` -- block a login, with a reason.
  A platform admin blocks in every tenant. A tenant admin blocks in their
  own tenant only. Nobody blocks their own login (422).
* ``DELETE /api/v1/principals/blocked/{sub}`` -- unblock. 404 when there
  is no block the admin may remove. A platform admin whom a tenant admin
  blocked in this tenant removes that block on their own login: this is
  their way back, also on an install with one tenant. On their own login
  the route removes only the block of this tenant, and it reads the
  table first. A block that a platform admin made (every tenant) stays
  until another platform admin removes it. A tenant admin who is
  blocked cannot remove a block.

Every route needs ``tenant_admin`` (the unblock route also takes a
platform admin of any tenant role) and a person's login: an agent,
service or runner login is refused with 403. Block and unblock write one
audit row each, with the admin, the login and the reason.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.principal_block import (
    PRINCIPAL_BLOCKED_MESSAGE,
    REASON_MAX_LENGTH,
    BlockedPerson,
    CannotBlockSelfError,
    OwnBlockStaysError,
    PrincipalBlockUnavailableError,
    PrincipalNotBlockedError,
    block_principal,
    is_principal_blocked,
    list_blocked,
    unblock_principal,
)
from meho_backplane.auth.rbac import ensure_human_principal, require_role
from meho_backplane.middleware import verify_jwt_and_bind_platform_admin_way_back

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/principals/blocked", tags=["principals"])

_SUB_MAX_LENGTH: Final[int] = 255
_PEOPLE_ONLY: Final[str] = (
    "only a person can block or unblock a login. Use the CLI (meho principal) or the REST API."
)


_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))


def _require_person_admin(operator: Operator = _require_admin) -> Operator:
    """``tenant_admin`` who signed in as a person."""
    ensure_human_principal(operator, remediation=_PEOPLE_ONLY)
    return operator


def _require_unblock_caller(
    operator: Operator = Depends(verify_jwt_and_bind_platform_admin_way_back),
) -> Operator:
    """A tenant admin or a platform admin, who signed in as a person."""
    if operator.tenant_role is not TenantRole.TENANT_ADMIN and not operator.platform_admin:
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="insufficient_role")
    ensure_human_principal(operator, remediation=_PEOPLE_ONLY)
    return operator


class BlockCreate(BaseModel):
    """Body of ``POST /api/v1/principals/blocked``."""

    model_config = ConfigDict(extra="forbid")

    sub: str = Field(min_length=1, max_length=_SUB_MAX_LENGTH)
    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)


class BlockRead(BaseModel):
    """One block of the list."""

    model_config = ConfigDict(frozen=True)

    sub: str
    scope: str
    reason: str
    blocked_by_sub: str
    blocked_at: datetime


class BlockListResponse(BaseModel):
    """Response of ``GET /api/v1/principals/blocked``."""

    model_config = ConfigDict(frozen=True)

    blocked: list[BlockRead]


class UnblockResponse(BaseModel):
    """Response of ``DELETE /api/v1/principals/blocked/{sub}``."""

    model_config = ConfigDict(frozen=True)

    sub: str
    removed: int


def _read(person: BlockedPerson) -> BlockRead:
    return BlockRead(
        sub=person.sub,
        scope=person.scope,
        reason=person.reason,
        blocked_by_sub=person.blocked_by_sub,
        blocked_at=person.blocked_at,
    )


@router.get("", response_model=BlockListResponse)
async def list_blocked_principals(
    operator: Operator = Depends(_require_person_admin),
) -> BlockListResponse:
    """List the blocked logins the admin may see."""
    structlog.contextvars.bind_contextvars(
        audit_op_id="principal.blocked.list", audit_op_class="read"
    )
    return BlockListResponse(blocked=[_read(p) for p in await list_blocked(operator)])


@router.post("", response_model=BlockRead, status_code=http_status.HTTP_201_CREATED)
async def block_principal_route(
    body: BlockCreate,
    operator: Operator = Depends(_require_person_admin),
) -> BlockRead:
    """Block a login. Blocking again in the same scope updates the reason."""
    structlog.contextvars.bind_contextvars(audit_op_id="principal.block", audit_op_class="write")
    try:
        return _read(await block_principal(operator, body.sub, body.reason))
    except CannotBlockSelfError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cannot_block_self: you cannot block your own login.",
        ) from exc


@router.delete("/{sub}", response_model=UnblockResponse)
async def unblock_principal_route(
    sub: Annotated[str, Path(min_length=1, max_length=_SUB_MAX_LENGTH)],
    operator: Operator = Depends(_require_unblock_caller),
) -> UnblockResponse:
    """Unblock a login. On their own login an admin removes this tenant's block only."""
    # A caller who is blocked in this tenant may remove only their own
    # block, and only as a platform admin: a blocked tenant admin never
    # reaches this point.
    structlog.contextvars.bind_contextvars(audit_op_id="principal.unblock", audit_op_class="write")
    try:
        if sub != operator.sub and await is_principal_blocked(operator.sub, operator.tenant_id):
            raise _blocked()
        removed = await unblock_principal(operator, sub)
    except PrincipalBlockUnavailableError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="principal_block_check_unavailable",
        ) from exc
    except OwnBlockStaysError as exc:
        raise _blocked() from exc
    except PrincipalNotBlockedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="principal_not_blocked",
        ) from exc
    return UnblockResponse(sub=sub, removed=removed)


def _blocked() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail=f"principal_blocked: {PRINCIPAL_BLOCKED_MESSAGE}",
    )
