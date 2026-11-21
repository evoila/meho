# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/targets/outside-tenant-folder``: targets whose secret path is outside the folder.

MEHO keeps each tenant's secrets under the tenant's own folder in the
secret store. This route lists the live targets whose own ``secret_ref``
points outside that folder, so an admin can move the secrets into it.

* A ``tenant_admin`` sees the targets of their own tenant.
* A platform admin (a person) sees the targets of every tenant, or of one
  tenant with ``?tenant_id=<uuid>``.
* When the folder check is off on this install, the list is empty and
  ``check_off`` is true.

Only Vault and OpenBao paths are checked. A path that MEHO built from the
target name is inside the folder by design and is not listed. The answer
holds names, tenant ids, the path and the reason, never a secret value.
The list is read from the targets table when asked.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole, is_human_principal
from meho_backplane.auth.rbac import require_role
from meho_backplane.connectors._shared.credential_backend import split_credential_ref
from meho_backplane.connectors._shared.vault_creds import DEFAULT_KV_MOUNT
from meho_backplane.connectors.vault.tenant_paths import is_derived_secret_ref
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
    if is_derived_secret_ref(ref, tenant_id=row.tenant_id, target_name=row.name):
        return None
    return outside_tenant_folder_reason(
        row.tenant_id, mount=DEFAULT_KV_MOUNT, path=store_ref, read_only=True
    )


@router.get("/outside-tenant-folder", response_model=OutsideTenantFolderList)
async def outside_tenant_folder(
    tenant_id: UUID | None = Query(default=None),
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> OutsideTenantFolderList:
    """List the live targets whose own secret path is outside the tenant's folder.

    Registered before ``GET /{name}`` so the literal segment is not read as
    a target name.
    """
    platform_admin = operator.platform_admin and is_human_principal(operator)
    if tenant_id is not None and tenant_id != operator.tenant_id and not platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only a platform admin can list another tenant",
        )
    scope: UUID | None = tenant_id if platform_admin else operator.tenant_id
    all_tenants = scope is None
    check_off = _folder_check_is_off(scope)
    if check_off:
        return OutsideTenantFolderList(
            all_tenants=all_tenants, check_off=True, message=CHECK_OFF_MESSAGE, items=[]
        )
    stmt = select(TargetORM).where(TargetORM.deleted_at.is_(None))
    if scope is not None:
        stmt = stmt.where(TargetORM.tenant_id == scope)
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
