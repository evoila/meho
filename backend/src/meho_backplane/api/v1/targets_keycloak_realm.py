# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/targets/checks/keycloak-realm``: Keycloak targets MEHO refuses.

A Keycloak target must name a managed realm, and both its realms must be
plain names (:mod:`meho_backplane.targets.required_extras`). MEHO refuses
every operation on a target that does not. This route lists such targets,
so an operator can fix them after an upgrade; ``meho doctor`` shows the
list.

* A caller with the ``operator`` role sees the targets of their tenant.
* A platform admin (a person) sees the targets of every tenant.

The answer holds names, tenant ids and the reason only, never a secret.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole, is_human_principal
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.targets.required_extras import KEYCLOAK_PRODUCT, keycloak_realm_problem

__all__ = ["KeycloakRealmCheck", "KeycloakRealmProblem", "router"]

router = APIRouter(prefix="/api/v1/targets", tags=["targets"])

_require_operator = Depends(require_role(TenantRole.OPERATOR))


class KeycloakRealmProblem(BaseModel):
    """One Keycloak target MEHO refuses, and why."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    name: str
    problem: str


class KeycloakRealmCheck(BaseModel):
    """The Keycloak targets MEHO refuses; ``all_tenants`` says how far the list goes."""

    model_config = ConfigDict(frozen=True)

    all_tenants: bool
    items: list[KeycloakRealmProblem]


@router.get("/checks/keycloak-realm", response_model=KeycloakRealmCheck)
async def keycloak_realm_check(
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> KeycloakRealmCheck:
    """List the live Keycloak targets with no managed realm, or a realm that is not a plain name.

    A platform admin who is a person gets the targets of every tenant;
    every other caller gets the targets of their own tenant.
    """
    all_tenants = operator.platform_admin and is_human_principal(operator)
    stmt = select(TargetORM).where(
        TargetORM.deleted_at.is_(None), TargetORM.product == KEYCLOAK_PRODUCT
    )
    if not all_tenants:
        stmt = stmt.where(TargetORM.tenant_id == operator.tenant_id)
    rows = (await session.execute(stmt.order_by(TargetORM.tenant_id, TargetORM.name))).scalars()
    items = [
        KeycloakRealmProblem(tenant_id=row.tenant_id, name=row.name, problem=problem)
        for row in rows
        if (problem := keycloak_realm_problem(row.extras, row.name)) is not None
    ]
    return KeycloakRealmCheck(all_tenants=all_tenants, items=items)
