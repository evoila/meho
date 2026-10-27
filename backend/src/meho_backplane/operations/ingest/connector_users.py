# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find what still uses a connector before it is deleted (#5027).

:func:`find_connector_users` returns every row that names a connector or
one of its operations:

* a **sensor** whose ``connector_id`` is the connector;
* a **runbook template** (draft or published) with a step or a verify
  call whose ``op_id`` is one of the connector's operations;
* a **hosted agent** whose ``toolset.connectors`` list names the connector;
* a **schedule** (active or paused) that starts one of those hosted agents;
* an **approval request** that waits, or is approved and can still run
  (see :func:`_approval_users`);
* an **operation run** that is queued or running.

The caller decides which tenants count. The delete in
:mod:`meho_backplane.operations.ingest.delete_connector` reads it inside
its own transaction, after it took the exclusive connector-usage lock
(:mod:`meho_backplane.db.connector_usage_lock`).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import (
    AgentDefinition,
    ApprovalRequest,
    ApprovalRequestStatus,
    OperationRun,
    OperationRunStatus,
    RunbookTemplate,
    ScheduledTrigger,
    ScheduledTriggerStatus,
    Sensor,
)
from meho_backplane.operations.ingest.exceptions import ConnectorUser
from meho_backplane.settings import get_settings

__all__ = ["TenantFilter", "find_connector_users"]


class TenantFilter:
    """Which tenants' rows count: a fixed list, or every tenant except some."""

    def __init__(
        self,
        *,
        only: Iterable[UUID] | None = None,
        exclude: Iterable[UUID] = (),
    ) -> None:
        self.only = frozenset(only) if only is not None else None
        self.exclude = frozenset(exclude)

    def clause(self, column: Any) -> ColumnElement[bool]:
        if self.only is not None:
            return column.in_(sorted(self.only, key=str))  # type: ignore[no-any-return]
        if self.exclude:
            return column.not_in(sorted(self.exclude, key=str))  # type: ignore[no-any-return]
        return true()


def _template_op_ids(steps: object, op_ids: frozenset[str]) -> list[str]:
    """Return the sorted op ids of *op_ids* that a template's steps call."""
    found: set[str] = set()
    if not isinstance(steps, list):
        return []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("type") == "operation_call" and step.get("op_id") in op_ids:
            found.add(str(step["op_id"]))
        verify = step.get("verify")
        if (
            isinstance(verify, dict)
            and verify.get("type") == "operation_call"
            and verify.get("op_id") in op_ids
        ):
            found.add(str(verify["op_id"]))
    return sorted(found)


def _names_connector(toolset: object, connector_id: str) -> bool:
    if not isinstance(toolset, dict):
        return False
    connectors = toolset.get("connectors")
    return isinstance(connectors, list) and connector_id in connectors


async def _sensor_users(
    session: AsyncSession, connector_id: str, tenants: TenantFilter
) -> list[ConnectorUser]:
    rows = await session.execute(
        select(Sensor.id, Sensor.name, Sensor.op_id, Sensor.tenant_id)
        .where(Sensor.connector_id == connector_id, tenants.clause(Sensor.tenant_id))
        .order_by(Sensor.name)
    )
    return [
        ConnectorUser(
            kind="sensor", id=str(row.id), name=row.name, op_id=row.op_id, tenant_id=row.tenant_id
        )
        for row in rows
    ]


async def _runbook_users(
    session: AsyncSession, op_ids: frozenset[str], tenants: TenantFilter
) -> list[ConnectorUser]:
    if not op_ids:
        return []
    rows = await session.execute(
        select(
            RunbookTemplate.id,
            RunbookTemplate.slug,
            RunbookTemplate.version,
            RunbookTemplate.steps,
            RunbookTemplate.tenant_id,
        )
        .where(
            RunbookTemplate.status.in_(("draft", "published")),
            tenants.clause(RunbookTemplate.tenant_id),
        )
        .order_by(RunbookTemplate.slug, RunbookTemplate.version)
    )
    users: list[ConnectorUser] = []
    for row in rows:
        for op_id in _template_op_ids(row.steps, op_ids):
            users.append(
                ConnectorUser(
                    kind="runbook_template",
                    id=str(row.id),
                    name=f"{row.slug} v{row.version}",
                    op_id=op_id,
                    tenant_id=row.tenant_id,
                )
            )
    return users


async def _agent_and_schedule_users(
    session: AsyncSession, connector_id: str, tenants: TenantFilter
) -> list[ConnectorUser]:
    rows = await session.execute(
        select(
            AgentDefinition.id,
            AgentDefinition.name,
            AgentDefinition.toolset,
            AgentDefinition.tenant_id,
        )
        .where(tenants.clause(AgentDefinition.tenant_id))
        .order_by(AgentDefinition.name)
    )
    agents = [row for row in rows if _names_connector(row.toolset, connector_id)]
    users = [
        ConnectorUser(
            kind="hosted_agent", id=str(row.id), name=row.name, op_id=None, tenant_id=row.tenant_id
        )
        for row in agents
    ]
    if not agents:
        return users
    agent_names = {row.id: row.name for row in agents}
    triggers = await session.execute(
        select(
            ScheduledTrigger.id,
            ScheduledTrigger.kind,
            ScheduledTrigger.agent_definition_id,
            ScheduledTrigger.tenant_id,
        )
        .where(
            ScheduledTrigger.agent_definition_id.in_(list(agent_names)),
            ScheduledTrigger.status.in_(
                (ScheduledTriggerStatus.ACTIVE.value, ScheduledTriggerStatus.PAUSED.value)
            ),
        )
        .order_by(ScheduledTrigger.created_at)
    )
    users.extend(
        ConnectorUser(
            kind="schedule",
            id=str(row.id),
            name=f"{row.kind} schedule for hosted agent {agent_names[row.agent_definition_id]}",
            op_id=None,
            tenant_id=row.tenant_id,
        )
        for row in triggers
    )
    return users


async def _approval_users(
    session: AsyncSession, connector_id: str, tenants: TenantFilter
) -> list[ConnectorUser]:
    """Approval requests that wait, or are approved and can still run.

    An approved request counts only while all three hold: nobody started
    it (``resumed_at`` is empty), it has no stored result
    (``resume_result`` is empty), and it was approved within the approval
    queue's default time limit (``approval_default_ttl_seconds``). Rows
    approved before ``resumed_at`` existed, and rows whose agent stopped
    waiting, keep ``resumed_at`` empty for ever. ``approved`` is a final
    status, so without the time limit such a row would block the delete
    for ever.
    """
    approved_after = datetime.now(UTC) - timedelta(
        seconds=get_settings().approval_default_ttl_seconds
    )
    rows = await session.execute(
        select(ApprovalRequest.id, ApprovalRequest.op_id, ApprovalRequest.tenant_id)
        .where(
            ApprovalRequest.connector_id == connector_id,
            tenants.clause(ApprovalRequest.tenant_id),
            or_(
                ApprovalRequest.status == ApprovalRequestStatus.PENDING.value,
                and_(
                    ApprovalRequest.status == ApprovalRequestStatus.APPROVED.value,
                    ApprovalRequest.resumed_at.is_(None),
                    ApprovalRequest.resume_result.is_(None),
                    ApprovalRequest.decided_at > approved_after,
                ),
            ),
        )
        .order_by(ApprovalRequest.created_at)
    )
    return [
        ConnectorUser(
            kind="approval_request",
            id=str(row.id),
            name=None,
            op_id=row.op_id,
            tenant_id=row.tenant_id,
        )
        for row in rows
    ]


async def _operation_run_users(
    session: AsyncSession, connector_id: str, tenants: TenantFilter
) -> list[ConnectorUser]:
    rows = await session.execute(
        select(OperationRun.id, OperationRun.op_id, OperationRun.tenant_id)
        .where(
            OperationRun.connector_id == connector_id,
            tenants.clause(OperationRun.tenant_id),
            OperationRun.status.in_(
                (OperationRunStatus.PENDING.value, OperationRunStatus.RUNNING.value)
            ),
        )
        .order_by(OperationRun.created_at)
    )
    return [
        ConnectorUser(
            kind="operation_run",
            id=str(row.id),
            name=None,
            op_id=row.op_id,
            tenant_id=row.tenant_id,
        )
        for row in rows
    ]


async def find_connector_users(
    session: AsyncSession,
    *,
    connector_id: str,
    op_ids: frozenset[str],
    tenants: TenantFilter,
) -> list[ConnectorUser]:
    """Return every row in *tenants* that names *connector_id* or one of *op_ids*."""
    users: list[ConnectorUser] = []
    users += await _sensor_users(session, connector_id, tenants)
    users += await _runbook_users(session, op_ids, tenants)
    users += await _agent_and_schedule_users(session, connector_id, tenants)
    users += await _approval_users(session, connector_id, tenants)
    users += await _operation_run_users(session, connector_id, tenants)
    return users
