# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The personal agent rules in the import plan (``CUSTOM_POLICY`` on only).

:func:`agents_plan` adds to the diff of :func:`~meho_backplane.policy.service.build_plan`:

* for a tenant's file: ``agents`` (the rules added, changed and removed)
  and ``runs_at_once`` (for each role, the operations that start to run at
  once for a personal agent under the file's rules). Both only when one
  side has a rule.
* for the install's file: ``agents_above_limit``, each tenant with a stored
  rule above the file's outer limit, with one warning each. Only when
  there is such a tenant.

It reads the database and writes nothing. The rules themselves are in
:mod:`meho_backplane.policy.agent_rules`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, TenantPolicy
from meho_backplane.op_classes import is_root_shell
from meho_backplane.operations.grant_approval import is_rights_change_op
from meho_backplane.operations.op_policy import has_plain_mark
from meho_backplane.operations.role_floors import role_floor_for
from meho_backplane.operations.service_grants import never_covered_reason
from meho_backplane.policy.agent_rules import (
    RULE_ROLES,
    RuleOp,
    agents_diff,
    rules_above_limit,
    rules_from_document,
    runs_at_once_by_role,
)
from meho_backplane.policy.classification import (
    RISK_LEVELS,
    Classification,
    ClassificationEntry,
    OpCoordinates,
    Scope,
    Taxonomy,
    classify,
)
from meho_backplane.policy.limits import personal_agent_limit
from meho_backplane.policy.store import connector_id_of

__all__ = ["agents_plan"]

#: The rule role of each tenant role.
_RULE_ROLE: dict[TenantRole, str] = {
    TenantRole.READ_ONLY: "Reader",
    TenantRole.OPERATOR: "Operator",
    TenantRole.TENANT_ADMIN: "Admin",
}


def _keeps_waiting(row: EndpointDescriptor, classified: Classification) -> bool:
    """True when a check that no rule lowers makes every call of *row* wait."""
    return (
        "destructive" in (row.safety_level, classified.risk)
        or classified.always_wait
        or classified.conflict is not None
        or never_covered_reason(row) is not None
        or is_rights_change_op(row.op_id)
        or is_root_shell(row.op_id)
        or row.hard_to_undo is not None
        or row.source_kind == "composite"
        or (bool(row.requires_approval) and not has_plain_mark(row))
    )


def _level(row: EndpointDescriptor, classified: Classification) -> str:
    """The level the rule compares: the highest of the code, the policy and the plain mark."""
    levels = [row.safety_level, classified.risk]
    if row.requires_approval and has_plain_mark(row):
        levels.append("dangerous")
    return max((lvl for lvl in levels if lvl in RISK_LEVELS), key=RISK_LEVELS.index, default="safe")


def _lowest_role(op_id: str) -> str:
    """The lowest rule role that may call *op_id* (its role floor, or ``Reader``)."""
    floor = role_floor_for(op_id)
    return RULE_ROLES[0] if floor is None else _RULE_ROLE.get(floor, RULE_ROLES[-1])


async def _rule_ops(
    session: AsyncSession,
    tenant_id: uuid.UUID | None,
    entries: list[ClassificationEntry],
    taxonomy: Taxonomy,
) -> list[RuleOp]:
    """Each enabled operation the tenant can call, as the rule check sees it."""
    group = aliased(OperationGroup)
    visible: ColumnElement[bool] = EndpointDescriptor.tenant_id.is_(None)
    if tenant_id is not None:
        visible = or_(visible, EndpointDescriptor.tenant_id == tenant_id)
    stmt = (
        select(EndpointDescriptor, group.group_key)
        .outerjoin(group, EndpointDescriptor.group_id == group.id)
        .where(visible, EndpointDescriptor.is_enabled.is_(True))
    )
    ops: list[RuleOp] = []
    for row, group_key in (await session.execute(stmt)).tuples():
        connector_id = connector_id_of(row)
        classified = classify(OpCoordinates(connector_id, group_key, row.op_id), entries, taxonomy)
        ops.append(
            RuleOp(
                connector_id=connector_id,
                op_id=row.op_id,
                level=_level(row, classified),
                changes_rights=bool(row.changes_rights),
                keeps_waiting=_keeps_waiting(row, classified),
                lowest_role=_lowest_role(row.op_id),
            )
        )
    return ops


async def _tenants_above_limit(session: AsyncSession, limit: str) -> list[dict[str, Any]]:
    rows = await session.execute(select(TenantPolicy).where(TenantPolicy.tenant_id.is_not(None)))
    found: list[dict[str, Any]] = []
    for row in rows.scalars():
        sentences = rules_above_limit(row.document, limit, place=f"tenant {row.tenant_id}:agents")
        if sentences:
            found.append({"tenant_id": str(row.tenant_id), "entries": sentences})
    found.sort(key=lambda item: str(item["tenant_id"]))
    return found


async def agents_plan(
    session: AsyncSession,
    *,
    scope: Scope,
    tenant_id: uuid.UUID | None,
    before: dict[str, Any] | None,
    document: dict[str, Any],
    install_document: dict[str, Any] | None,
    side: tuple[list[ClassificationEntry], Taxonomy],
    custom_policy: bool,
) -> tuple[dict[str, Any], list[str]]:
    """The diff sections and the warnings of the personal agent rules.

    *document* is the planned document, without mistakes. *side* holds the
    classification entries and the taxonomy after the import (the plan's
    new side). With the ``CUSTOM_POLICY`` switch off, nothing is added.
    """
    if not custom_policy:
        return {}, []
    if scope == "install":
        limit = personal_agent_limit(document, custom_policy=True)
        found = await _tenants_above_limit(session, limit)
        warnings = [
            f"Tenant {item['tenant_id']}: {sentence}"
            for item in found
            for sentence in item["entries"]
        ]
        return ({"agents_above_limit": found} if found else {}), warnings
    diff = agents_diff(before, document)
    if not diff:
        return {}, []
    limit = personal_agent_limit(install_document, custom_policy=True)
    ops = await _rule_ops(session, tenant_id, *side)
    diff["runs_at_once"] = runs_at_once_by_role(rules_from_document(document), limit=limit, ops=ops)
    return diff, []
