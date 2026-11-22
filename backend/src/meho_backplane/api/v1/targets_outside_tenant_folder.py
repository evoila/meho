# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/targets/checks/outside-tenant-folder``: targets with a path outside the folder.

MEHO keeps each tenant's secrets under the tenant's own folder in the
secret store. This route lists the live targets whose own ``secret_ref``
points outside that folder, so an admin can move the secrets into it.

* A ``tenant_admin`` sees the targets of their own tenant.
* A platform admin (a person) sees the targets of every tenant, or of one
  tenant with ``?tenant_id=<uuid>``. A tenant the admin is a member of
  is chosen with the ``Meho-Tenant`` header, so their role there applies
  (:func:`~meho_backplane.auth.rbac.authorize_tenant_scope`). The list of
  every tenant leaves out each tenant where the admin is a member without
  the ``tenant_admin`` role.
* When the folder check is off on this install, the list is empty and
  ``check_off`` is true.

Only Vault and OpenBao paths are checked, with the same rule as the
warning a read of the secret writes, so a target is listed exactly when
that read warns. The answer holds names, tenant ids, the path and the
reason, never a secret value.
The list is read from the targets table when asked.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole, group_grant, is_human_principal
from meho_backplane.auth.rbac import authorize_tenant_scope, require_role
from meho_backplane.connectors._shared.credential_backend import split_credential_ref
from meho_backplane.connectors._shared.vault_creds import DEFAULT_KV_MOUNT
from meho_backplane.connectors.vault.tenant_scope import (
    count_tenants_with_own_vault_role,
    effective_tenant_scope_template_for_tenant,
    outside_tenant_folder_reason,
)
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.settings import get_settings

__all__ = ["OutsideTenantFolderItem", "OutsideTenantFolderList", "router"]

router = APIRouter(prefix="/api/v1/targets", tags=["targets"])

_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

CHECK_OFF_MESSAGE = "The folder check is off on this install"


class OutsideTenantFolderItem(BaseModel):
    """One target whose secret path is outside the tenant's folder, and why."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    name: str
    secret_ref: str
    reason: str


class OutsideTenantFolderList(BaseModel):
    """The targets outside the folder; ``all_tenants`` says how far the list goes."""

    model_config = ConfigDict(frozen=True)

    all_tenants: bool
    check_off: bool
    message: str | None = None
    items: list[OutsideTenantFolderItem]


def _folder_check_is_off(tenant_id: UUID | None) -> bool:
    """Whether the folder check does nothing for *tenant_id*, or for every tenant when ``None``."""
    if tenant_id is not None:
        return not effective_tenant_scope_template_for_tenant(tenant_id)
    settings = get_settings()
    return (
        not settings.vault_kv_tenant_scope_prefix.strip()
        and count_tenants_with_own_vault_role(settings.vault_oidc_role_by_tenant) == 0
    )


def _outside_reason(row: TargetORM) -> str | None:
    """Return why *row*'s own secret path is outside its tenant's folder, else ``None``."""
    ref = (row.secret_ref or "").strip()
    if not ref:
        return None
    kind, store_ref = split_credential_ref(ref, default_backend=get_settings().credential_backend)
    if kind != "vault":
        return None
    return outside_tenant_folder_reason(
        row.tenant_id, mount=DEFAULT_KV_MOUNT, path=store_ref, read_only=True
    )


def _member_tenants_without_admin_role(operator: Operator) -> set[UUID]:
    """The tenants *operator* is a member of with a role below ``tenant_admin``."""
    return {
        entry.tenant_id
        for entry in operator.tenants
        if entry.tenant_id != operator.tenant_id
        and group_grant(entry.group).role is not TenantRole.TENANT_ADMIN
    }


@router.get("/checks/outside-tenant-folder", response_model=OutsideTenantFolderList)
async def outside_tenant_folder(
    tenant_id: UUID | None = Query(default=None),
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> OutsideTenantFolderList:
    """List the live targets whose own secret path is outside the tenant's folder."""
    platform_admin = operator.platform_admin and is_human_principal(operator)
    scope: UUID | None
    if tenant_id is not None or not platform_admin:
        scope = authorize_tenant_scope(operator, tenant_id)
    else:
        scope = None
    all_tenants = scope is None
    check_off = _folder_check_is_off(scope)
    if check_off:
        return OutsideTenantFolderList(
            all_tenants=all_tenants, check_off=True, message=CHECK_OFF_MESSAGE, items=[]
        )
    stmt = select(TargetORM).where(TargetORM.deleted_at.is_(None))
    if scope is not None:
        stmt = stmt.where(TargetORM.tenant_id == scope)
    else:
        hidden = _member_tenants_without_admin_role(operator)
        if hidden:
            stmt = stmt.where(TargetORM.tenant_id.not_in(hidden))
    rows = (await session.execute(stmt.order_by(TargetORM.tenant_id, TargetORM.name))).scalars()
    items = [
        OutsideTenantFolderItem(
            tenant_id=row.tenant_id,
            name=row.name,
            secret_ref=row.secret_ref or "",
            reason=reason,
        )
        for row in rows
        if (reason := _outside_reason(row)) is not None
    ]
    return OutsideTenantFolderList(all_tenants=all_tenants, check_off=False, items=items)
