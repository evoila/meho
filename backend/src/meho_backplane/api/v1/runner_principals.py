# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/runner-principals*`` — REST surface for runner-identity lifecycle.

Initiative #2415 (#2502) under Goal #221. Four routes that expose
:class:`~meho_backplane.auth.runner_principals.RunnerPrincipalService` to
operators. The Go CLI verbs (``meho runner-principal``) call into the same
service from their own transport; this module is the HTTP front. There is
deliberately **no** MCP tool front for runner-principal lifecycle in v1
(REST + CLI only).

Route inventory
---------------

* ``GET /api/v1/runner-principals`` — list active runner principals for
  the operator's tenant, name-sorted. Query params: ``limit``, ``offset``,
  ``include_revoked``. Role: ``operator``.
* ``GET /api/v1/runner-principals/{name}`` — show one principal by name;
  404 when absent. Role: ``operator``.
* ``POST /api/v1/runner-principals`` — register a new runner principal
  (creates the Keycloak client + inserts DB row). Returns the row with
  HTTP 201. 409 on duplicate ``(tenant, name)``. Role: ``tenant_admin``.
* ``DELETE /api/v1/runner-principals/{name}/revoke`` — revoke a runner
  (kill switch: disables Keycloak client + marks row revoked). Returns the
  updated row. Role: ``tenant_admin``.
* ``GET /api/v1/runner-principals/{name}/write-allowlist`` — list the
  runner's granted ``remote-write`` capabilities (#3190). Role: ``operator``.
* ``POST /api/v1/runner-principals/{name}/write-allowlist`` — grant one
  ``remote-write`` capability (op-pattern + target-scope). The separate human
  step a write capability requires after enrollment (a runner is never
  granted write capability at birth). Returns 201. Role: ``tenant_admin``.

Enforcement scope (the #2489 lesson, stated explicitly)
-------------------------------------------------------
Registering a runner principal mints a Keycloak client whose token carries
a hardcoded ``principal_kind=runner`` mapper and a read-only
``tenant_role``. That token is **caged**: the negative route cage in
:func:`~meho_backplane.middleware.verify_jwt_and_bind` fail-closed 403s a
``principal_kind=runner`` token on every authenticated route outside
:data:`~meho_backplane.middleware.RUNNER_ALLOWED_PATH_PREFIXES`
(``/api/v1/gateway/`` + ``/api/v1/checks/``), and the MCP surface rejects
it outright. The cage gates **only** ``principal_kind=runner`` tokens —
user / service / agent tokens are unaffected — so the enforcement is
fail-closed on the unforgeable discriminator, not on the presence of an
optional claim.

Tenant scoping
--------------
Every route derives ``tenant_id`` from the JWT-validated
:class:`~meho_backplane.auth.operator.Operator`; no surface accepts a
tenant id from the body or query string. Cross-tenant name probes surface
as 404 (never 403).
"""

from __future__ import annotations

from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict

from meho_backplane.auth.keycloak_admin import (
    KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL,
    KeycloakAdminError,
    KeycloakAdminNotConfiguredError,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.auth.runner_principals import (
    NAME_MAX_LENGTH,
    RunnerPrincipalCreate,
    RunnerPrincipalExistsError,
    RunnerPrincipalNotFoundError,
    RunnerPrincipalRead,
    RunnerPrincipalService,
)
from meho_backplane.auth.runner_write_allowlist import (
    RemoteWriteCapabilityGrant,
    RunnerWriteAllowlistEntryRead,
    RunnerWriteAllowlistService,
)
from meho_backplane.scheduler.vault_credentials import (
    SCHEDULER_VAULT_TOKEN_INVALID_DETAIL,
    SchedulerVaultBrokerError,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/runner-principals", tags=["runner-principals"])

_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

_OP_IDS: Final[dict[str, str]] = {
    "list": "runner_principal.list",
    "show": "runner_principal.show",
    "register": "runner_principal.register",
    "revoke": "runner_principal.revoke",
    "write_allowlist_list": "runner_principal.write_allowlist.list",
    "write_allowlist_grant": "runner_principal.write_allowlist.grant",
}


class RunnerPrincipalListResponse(BaseModel):
    """Response envelope for ``GET /api/v1/runner-principals``."""

    model_config = ConfigDict(frozen=True)

    principals: list[RunnerPrincipalRead]


class RunnerWriteAllowlistResponse(BaseModel):
    """Response envelope for ``GET .../{name}/write-allowlist``."""

    model_config = ConfigDict(frozen=True)

    entries: list[RunnerWriteAllowlistEntryRead]


def _handle_admin_error(exc: Exception) -> HTTPException:
    """Map Keycloak admin errors to HTTP responses.

    * :class:`KeycloakAdminNotConfiguredError` -> 503 with the
      gold-standard three-clause detail.
    * Any other :class:`KeycloakAdminError` -> 502 with the bare
      ``keycloak_admin_error`` code (the structured log carries the
      exception class + status for off-path diagnosis).
    """
    if isinstance(exc, KeycloakAdminNotConfiguredError):
        return HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL,
        )
    return HTTPException(
        status_code=http_status.HTTP_502_BAD_GATEWAY,
        detail="keycloak_admin_error",
    )


@router.get("", response_model=RunnerPrincipalListResponse)
async def list_runner_principals(
    operator: Operator = _require_operator,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_revoked: bool = Query(default=False),
) -> RunnerPrincipalListResponse:
    """List runner principals for the operator's tenant."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["list"],
        audit_op_class="read",
    )
    service = RunnerPrincipalService()
    principals = await service.list_(
        operator.tenant_id,
        include_revoked=include_revoked,
        limit=limit,
        offset=offset,
    )
    return RunnerPrincipalListResponse(principals=principals)


@router.get("/{name}", response_model=RunnerPrincipalRead)
async def show_runner_principal(
    name: Annotated[str, Path(max_length=NAME_MAX_LENGTH)],
    operator: Operator = _require_operator,
) -> RunnerPrincipalRead:
    """Return one runner principal by name."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["show"],
        audit_op_class="read",
    )
    service = RunnerPrincipalService()
    entry = await service.get(operator.tenant_id, name)
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="runner_principal_not_found",
        )
    return entry


@router.post(
    "",
    response_model=RunnerPrincipalRead,
    status_code=http_status.HTTP_201_CREATED,
)
async def register_runner_principal(
    body: RunnerPrincipalCreate,
    operator: Operator = _require_admin,
) -> RunnerPrincipalRead:
    """Register a new runner principal (creates Keycloak client + DB row).

    ``tenant_admin`` only. The minted Keycloak client's token carries a
    hardcoded ``principal_kind=runner`` mapper and a read-only
    ``tenant_role``, so the resulting principal is caged to the gateway
    path prefixes and rejected everywhere else (see the module docstring).
    Returns 409 on duplicate ``(tenant, name)``; 503 when Keycloak admin is
    not configured.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["register"],
        audit_op_class="write",
        audit_runner_principal_name=body.name,
    )
    service = RunnerPrincipalService()
    try:
        return await service.register(
            tenant_id=operator.tenant_id,
            created_by_sub=operator.sub,
            payload=body,
        )
    except RunnerPrincipalExistsError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="runner_principal_already_exists",
        ) from exc
    except KeycloakAdminError as exc:
        raise _handle_admin_error(exc) from exc
    except SchedulerVaultBrokerError as exc:
        # ``VAULT_SCHEDULER_TOKEN`` is set but the Vault write failed
        # (unreachable / denied). 502 upstream failure; the just-created
        # Keycloak client is already rolled back. The broker's
        # ``lookup-self`` probe (#2652) says whether the token is dead —
        # that case gets the gold-standard three-clause detail naming the
        # re-mint, because its remediation *is* knowable; everything else
        # keeps the bare ``scheduler_vault_write_error`` code.
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=(
                SCHEDULER_VAULT_TOKEN_INVALID_DETAIL
                if exc.token_invalid
                else "scheduler_vault_write_error"
            ),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.delete("/{name}/revoke", response_model=RunnerPrincipalRead)
async def revoke_runner_principal(
    name: Annotated[str, Path(max_length=NAME_MAX_LENGTH)],
    operator: Operator = _require_admin,
) -> RunnerPrincipalRead:
    """Revoke a runner principal (kill switch).

    Disables the Keycloak client (blocks new token grants) and marks the DB
    row ``revoked=true``. Returns the updated row. ``tenant_admin`` only.
    Returns 404 when absent or already revoked.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["revoke"],
        audit_op_class="write",
        audit_runner_principal_name=name,
    )
    service = RunnerPrincipalService()
    try:
        return await service.revoke(operator.tenant_id, name)
    except RunnerPrincipalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="runner_principal_not_found",
        ) from exc
    except KeycloakAdminError as exc:
        raise _handle_admin_error(exc) from exc


@router.get("/{name}/write-allowlist", response_model=RunnerWriteAllowlistResponse)
async def list_runner_write_allowlist(
    name: Annotated[str, Path(max_length=NAME_MAX_LENGTH)],
    operator: Operator = _require_operator,
) -> RunnerWriteAllowlistResponse:
    """List a runner's granted ``remote-write`` capabilities (#3190).

    Read-back of the per-runner capability allowlist (mechanism 2 of the
    satellite write path). 404 when the runner is unknown or revoked.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["write_allowlist_list"],
        audit_op_class="read",
        audit_runner_principal_name=name,
    )
    service = RunnerWriteAllowlistService()
    try:
        entries = await service.list_(operator.tenant_id, name)
    except RunnerPrincipalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="runner_principal_not_found",
        ) from exc
    return RunnerWriteAllowlistResponse(entries=entries)


@router.post(
    "/{name}/write-allowlist",
    response_model=RunnerWriteAllowlistEntryRead,
    status_code=http_status.HTTP_201_CREATED,
)
async def grant_runner_write_capability(
    name: Annotated[str, Path(max_length=NAME_MAX_LENGTH)],
    body: RemoteWriteCapabilityGrant,
    operator: Operator = _require_admin,
) -> RunnerWriteAllowlistEntryRead:
    """Grant one ``remote-write`` capability to a runner (#3190).

    The **separate human step** a write capability requires after enrollment:
    ``tenant_admin`` only, and never reachable by a runner's own read-only,
    route-caged token — so a runner cannot widen its own allowlist (threat T7,
    decision recommendation 2). Programmatic enrollment grants no write
    capability at birth; this route is the only path that adds one. The grant
    is bound at issuance (``created_by_sub`` records the operator) and
    idempotent on ``(op_pattern, target_scope)``. 404 when the runner is
    unknown or revoked.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["write_allowlist_grant"],
        audit_op_class="write",
        audit_runner_principal_name=name,
    )
    service = RunnerWriteAllowlistService()
    try:
        return await service.grant(
            tenant_id=operator.tenant_id,
            runner_name=name,
            created_by_sub=operator.sub,
            payload=body,
        )
    except RunnerPrincipalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="runner_principal_not_found",
        ) from exc
