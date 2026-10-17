# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant-authorized aggregate reads over request usage events (#3734).

Two read paths:

* :func:`summarize_request_usage` -- counts per transport, route and
  outcome for one tenant and time window. It resolves the tenant with
  :func:`~meho_backplane.auth.rbac.authorize_tenant_scope`, so a caller
  reads its own tenant, and only a platform admin may name another one.
  It returns counts only, never single events.
* :func:`select_finished_events` -- the row source the retrieval usage,
  retirement checklist and reflex adoption reports use when they read
  usage events instead of ``audit_log`` wrapper rows. Those reports'
  routes already authorize the tenant before they call it. The columns
  are labelled like the ``audit_log`` columns those reports read
  (``operator_sub``, ``path``, ``agent_session_id``), so the report
  logic stays the same for both sources.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.rbac import authorize_tenant_scope
from meho_backplane.db.models import RequestUsageEvent

__all__ = [
    "MCP_TOOL_ROUTE_PREFIX",
    "RequestUsageSummary",
    "RouteUsage",
    "UsageSource",
    "mcp_tool_route",
    "select_finished_events",
    "summarize_request_usage",
]

#: Where a report reads its activity rows from. ``audit_log`` is the
#: wrapper rows (the default until the reader cutover); ``request_usage``
#: is the usage events this package collects.
UsageSource = Literal["audit_log", "request_usage"]

#: Route label prefix for an MCP tool call (see the collector).
MCP_TOOL_ROUTE_PREFIX: Final[str] = "tool:"


def mcp_tool_route(tool_name: str) -> str:
    """Return the usage route label for MCP tool *tool_name*."""
    return f"{MCP_TOOL_ROUTE_PREFIX}{tool_name}"


class RouteUsage(BaseModel):
    """Request count for one ``(transport, route, outcome)`` group."""

    model_config = ConfigDict(frozen=True)

    transport: str
    route: str
    outcome: str
    requests: int
    distinct_principals: int


class RequestUsageSummary(BaseModel):
    """Aggregate request usage for one tenant and window."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    since: datetime
    until: datetime
    total_requests: int
    routes: list[RouteUsage]


def select_finished_events(
    *,
    since: datetime,
    until: datetime,
    tenant_id: UUID | None,
    routes: list[str] | None = None,
    ok_only: bool = False,
) -> Select[Any]:
    """Build the ``request_finished`` row query used by the usage reports.

    Columns: ``id``, ``occurred_at``, ``operator_sub``, ``tenant_id``,
    ``path`` (the route label) and ``agent_session_id`` (the session id).
    ``tenant_id=None`` means no tenant filter; only callers that already
    authorized a cross-tenant read may pass it. ``routes`` limits the
    route labels; ``ok_only`` keeps only ``ok`` outcomes. Rows come
    ordered by ``(operator_sub, occurred_at)``.
    """
    stmt = (
        select(
            RequestUsageEvent.id.label("id"),
            RequestUsageEvent.occurred_at.label("occurred_at"),
            RequestUsageEvent.principal_sub.label("operator_sub"),
            RequestUsageEvent.tenant_id.label("tenant_id"),
            RequestUsageEvent.route.label("path"),
            RequestUsageEvent.session_id.label("agent_session_id"),
        )
        .where(RequestUsageEvent.kind == "request_finished")
        .where(RequestUsageEvent.occurred_at >= since)
        .where(RequestUsageEvent.occurred_at <= until)
        .order_by(RequestUsageEvent.principal_sub, RequestUsageEvent.occurred_at)
    )
    if tenant_id is not None:
        stmt = stmt.where(RequestUsageEvent.tenant_id == tenant_id)
    if routes is not None:
        stmt = stmt.where(RequestUsageEvent.route.in_(routes))
    if ok_only:
        stmt = stmt.where(RequestUsageEvent.outcome == "ok")
    return stmt


async def summarize_request_usage(
    session: AsyncSession,
    *,
    operator: Operator,
    tenant_filter: UUID | None,
    since: datetime,
    until: datetime,
) -> RequestUsageSummary:
    """Count ``request_finished`` events per transport, route and outcome.

    The tenant comes from :func:`authorize_tenant_scope`: the caller's
    own tenant by default; another tenant only for a platform admin
    (anyone else gets HTTP 403). Read-only; the caller owns *session*.
    """
    tenant_id = authorize_tenant_scope(operator, tenant_filter)
    stmt = (
        select(
            RequestUsageEvent.transport,
            RequestUsageEvent.route,
            RequestUsageEvent.outcome,
            func.count(),
            func.count(distinct(RequestUsageEvent.principal_sub)),
        )
        .where(RequestUsageEvent.kind == "request_finished")
        .where(RequestUsageEvent.tenant_id == tenant_id)
        .where(RequestUsageEvent.occurred_at >= since)
        .where(RequestUsageEvent.occurred_at <= until)
        .group_by(
            RequestUsageEvent.transport,
            RequestUsageEvent.route,
            RequestUsageEvent.outcome,
        )
        .order_by(
            RequestUsageEvent.transport,
            RequestUsageEvent.route,
            RequestUsageEvent.outcome,
        )
    )
    routes = [
        RouteUsage(
            transport=transport,
            route=route,
            outcome=outcome or "",
            requests=count,
            distinct_principals=principals,
        )
        for transport, route, outcome, count, principals in (await session.execute(stmt)).all()
    ]
    return RequestUsageSummary(
        tenant_id=tenant_id,
        since=since,
        until=until,
        total_requests=sum(item.requests for item in routes),
        routes=routes,
    )
