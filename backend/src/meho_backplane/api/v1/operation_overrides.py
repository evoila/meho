# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/connectors/operation-overrides`` — the override report (#4711).

Lists the operator overrides of risk levels and approval flags
(:class:`~meho_backplane.db.models.OperationPolicyOverride`). With
``reduced_approval=true`` it lists only the operations a platform admin
turned approval off for: the report "operations running with reduced
approval". The CLI verb is ``meho connector overrides``; the console page is
``/ui/connectors/overrides``.

Role: ``tenant_admin``. A tenant admin sees the install-wide overrides and
their own tenant's; a platform admin sees every tenant's.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationPolicyOverride
from meho_backplane.operations.ingest.api_schemas import (
    OperationOverrideList,
    OperationOverrideRow,
)
from meho_backplane.operations.op_policy import list_overrides
from meho_backplane.policy.store import connector_id_of, product_form_on

__all__ = ["load_override_rows", "router"]

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))
_reduced_approval_query = Query(
    default=False,
    description="Only the operations a platform admin turned approval off for.",
)


def _connector_id(row: OperationPolicyOverride) -> str:
    """The policy's connector id with ``CUSTOM_POLICY`` on.

    With the switch off: ``<impl_id>-<version>``, or ``impl_id`` alone
    without a version, as before the policy named a connector by product.
    """
    if product_form_on():
        return connector_id_of(row)
    return f"{row.impl_id}-{row.version}" if row.version else row.impl_id


async def load_override_rows(
    operator: Operator, *, reduced_approval: bool
) -> list[OperationOverrideRow]:
    """Return the override rows *operator* may see. Shared with the console page."""
    async with get_sessionmaker()() as session:
        rows = await list_overrides(
            session, operator=operator, reduced_approval_only=reduced_approval
        )
    return [
        OperationOverrideRow(
            connector_id=_connector_id(row),
            op_id=row.op_id,
            scope="install" if row.tenant_id is None else "tenant",
            tenant_id=row.tenant_id,
            safety_level=policy.safety_level,
            requires_approval=policy.requires_approval,
            reason=row.reason,
            set_by_sub=row.set_by_sub,
            set_at=row.set_at,
            category=policy.category,
            labels=dict(policy.labels),
            always_wait=policy.always_wait,
            rule_ref=policy.rule_ref,
        )
        for row, policy in rows
    ]


@router.get("/operation-overrides", response_model=OperationOverrideList)
async def list_operation_overrides(
    reduced_approval: bool = _reduced_approval_query,
    operator: Operator = _require_admin,
) -> OperationOverrideList:
    """List the operator overrides of risk levels and approval flags.

    ``reduced_approval=true`` keeps only the operations whose approval a
    platform admin turned off and where approval is still off. Each row
    names the connector, the operation, the risk level and approval that
    apply in the row's scope, the scope, who set it, when, and the reason.
    With the ``CUSTOM_POLICY`` or ``POLICY_CLASSIFICATION`` switch on, the
    values include the classification, and ``rule_ref`` names the entry that decided it.
    """
    return OperationOverrideList(
        overrides=await load_override_rows(operator, reduced_approval=reduced_approval)
    )
