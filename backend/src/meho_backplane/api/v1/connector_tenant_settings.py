# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/connectors/{connector_id}/tenant-settings`` — per-tenant on/off (#5029).

A built-in operation's ``is_enabled`` flag is the install default, shared by
every tenant. These routes let one tenant turn built-in operations on or off
for itself only. The logic lives in
:mod:`meho_backplane.operations.tenant_operation_settings`.

Route inventory
---------------

* ``GET /api/v1/connectors/{connector_id}/tenant-settings`` — the
  operations the tenant has its own setting for, each with the install
  default (``default_enabled``), the tenant setting (``tenant_enabled``) and
  the effective value (``enabled``). Role: ``operator``.
* ``PUT /api/v1/connectors/{connector_id}/tenant-settings/operations/{op_id}``
  — set the tenant setting for one operation. Body:
  :class:`TenantSettingBody` (``enabled``: ``true`` / ``false`` / ``null``
  for "use the install default"). Role: ``tenant_admin``.
* ``PUT /api/v1/connectors/{connector_id}/tenant-settings/groups/{group_key}``
  — the same for every operation in one group. Role: ``tenant_admin``.

Tenant scoping: every route acts on the caller's own tenant. The optional
``tenant_id`` query parameter names another tenant; that needs
``platform_admin`` (403 ``cross_tenant_requires_platform_admin`` otherwise,
:func:`~meho_backplane.auth.rbac.authorize_tenant_scope`).

Errors: 404 when the connector has no built-in operations or the operation /
group does not exist; 409 ``tenant_owns_connector`` when the tenant has its
own copy of the connector (its own rows are then the switch). Every change
writes one audit row (``meho_connector_tenant_setting``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import authorize_tenant_scope, require_role
from meho_backplane.operations.tenant_operation_settings import (
    TenantOperationSettingItem,
    TenantOperationSettingNotFoundError,
    TenantOperationSettingsResponse,
    TenantOwnsConnectorError,
    list_operation_settings,
    set_group_setting,
    set_operation_setting,
)

__all__ = ["TenantSettingBody", "router"]

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))
_TENANT_QUERY = Query(
    default=None,
    description=(
        "Act on this tenant instead of your own. Needs platform_admin when it is not your tenant."
    ),
)


class TenantSettingBody(BaseModel):
    """Body of the tenant-setting writes.

    ``enabled`` is required: ``true`` turns the operation on for the tenant,
    ``false`` turns it off, ``null`` removes the setting so the tenant uses
    the install default again.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None


class TenantGroupSettingResponse(BaseModel):
    """The group's operations after a group write."""

    model_config = ConfigDict(frozen=True)

    connector_id: str
    tenant_id: UUID
    group_key: str
    items: list[TenantOperationSettingItem]


def _http_error(
    exc: TenantOwnsConnectorError | TenantOperationSettingNotFoundError,
) -> HTTPException:
    """Map a service error to 409 (tenant has its own copy) or 404."""
    if isinstance(exc, TenantOwnsConnectorError):
        return HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"detail": "tenant_owns_connector", "message": exc.message},
        )
    return HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=exc.message)


@router.get(
    "/{connector_id}/tenant-settings",
    response_model=TenantOperationSettingsResponse,
)
async def list_tenant_settings_endpoint(
    connector_id: str,
    operator: Operator = _require_operator,
    tenant_id: UUID | None = _TENANT_QUERY,
) -> TenantOperationSettingsResponse:
    """List the operations the tenant turned on or off for itself, with the install default."""
    target_tenant = authorize_tenant_scope(operator, tenant_id)
    try:
        return await list_operation_settings(tenant_id=target_tenant, connector_id=connector_id)
    except (TenantOwnsConnectorError, TenantOperationSettingNotFoundError) as exc:
        raise _http_error(exc) from exc


@router.put(
    "/{connector_id}/tenant-settings/operations/{op_id:path}",
    response_model=TenantOperationSettingItem,
)
async def set_tenant_operation_setting_endpoint(
    connector_id: str,
    op_id: str,
    body: TenantSettingBody,
    operator: Operator = _require_admin,
    tenant_id: UUID | None = _TENANT_QUERY,
) -> TenantOperationSettingItem:
    """Turn one built-in operation on or off for the tenant, or back to the install default.

    The ``op_id`` path parameter uses the ``:path`` converter so an op id
    with slashes (``GET:/api/vcenter/cluster``) round-trips.
    """
    target_tenant = authorize_tenant_scope(operator, tenant_id)
    try:
        return await set_operation_setting(
            operator,
            tenant_id=target_tenant,
            connector_id=connector_id,
            op_id=op_id,
            enabled=body.enabled,
        )
    except (TenantOwnsConnectorError, TenantOperationSettingNotFoundError) as exc:
        raise _http_error(exc) from exc


@router.put(
    "/{connector_id}/tenant-settings/groups/{group_key}",
    response_model=TenantGroupSettingResponse,
)
async def set_tenant_group_setting_endpoint(
    connector_id: str,
    group_key: str,
    body: TenantSettingBody,
    operator: Operator = _require_admin,
    tenant_id: UUID | None = _TENANT_QUERY,
) -> TenantGroupSettingResponse:
    """Turn every built-in operation in one group on or off for the tenant, or back to default."""
    target_tenant = authorize_tenant_scope(operator, tenant_id)
    try:
        items = await set_group_setting(
            operator,
            tenant_id=target_tenant,
            connector_id=connector_id,
            group_key=group_key,
            enabled=body.enabled,
        )
    except (TenantOwnsConnectorError, TenantOperationSettingNotFoundError) as exc:
        raise _http_error(exc) from exc
    return TenantGroupSettingResponse(
        connector_id=connector_id,
        tenant_id=target_tenant,
        group_key=group_key,
        items=items,
    )
